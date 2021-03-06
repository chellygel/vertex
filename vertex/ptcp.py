# -*- test-case-name: vertex.test.test_ptcp -*-

from __future__ import print_function

import struct

from binascii import crc32  # used to use zlib.crc32 - but that gives different
                            # results on 64-bit platforms!!

import itertools

from tcpdfa import TCP

from twisted.python.failure import Failure
from twisted.internet.defer import Deferred
from twisted.internet import protocol, error, reactor, defer
from twisted.internet.main import CONNECTION_DONE
from twisted.python import log, util

genConnID = itertools.count(8).next

MAX_PSEUDO_PORT = (2 ** 16)

_packetFormat = ('!' # WTF did you think
                 'H' # sourcePseudoPort
                 'H' # destPseudoPort
                 'L' # sequenceNumber
                 'L' # acknowledgementNumber
                 'L' # window
                 'B' # flags
                 'l' # checksum
                     # (signed because of binascii.crc32)
                 'H' # dlen
                 )
_fixedSize = struct.calcsize(_packetFormat)

SEND_DELAY = 0.00001
ACK_DELAY = 0.00001

_SYN, _ACK, _FIN, _RST, _STB = [1 << n for n in range(5)]

def _flagprop(flag):
    def setter(self, value):
        if value:
            self.flags |= flag
        else:
            self.flags &= ~flag
    return property(lambda self: bool(self.flags & flag), setter)

def relativeSequence(wireSequence, initialSequence, lapNumber):
    """ Compute a relative sequence number from a wire sequence number so that we
    can use natural Python comparisons on it, such as <, >, ==.

    @param wireSequence: the sequence number received on the wire.

    @param initialSequence: the ISN for this sequence, negotiated at SYN time.

    @param lapNumber: the number of times that this value has wrapped around
    2**32.
    """
    return (wireSequence + (lapNumber * (2**32))) - initialSequence

class PTCPPacket(util.FancyStrMixin, object):
    showAttributes = (
        ('sourcePseudoPort', 'sourcePseudoPort', '%d'),
        ('destPseudoPort', 'destPseudoPort', '%d'),
        ('shortdata', 'data', '%r'),
        ('niceflags', 'flags', '%s'),
        ('dlen', 'dlen', '%d'),
        ('seqNum', 'seq', '%d'),
        ('ackNum', 'ack', '%d'),
        ('checksum', 'checksum', '%x'),
        ('peerAddressTuple', 'peerAddress', '%r'),
        ('retransmitCount', 'retransmitCount', '%d'),
        )

    syn = _flagprop(_SYN)
    ack = _flagprop(_ACK)
    fin = _flagprop(_FIN)
    rst = _flagprop(_RST)
    stb = _flagprop(_STB)

    # Number of retransmit attempts left for this segment.  When it reaches
    # zero, this segment is dead.
    retransmitCount = 50

    def shortdata():
        def get(self):
            if len(self.data) > 13:
                return self.data[:5] + '...' + self.data[-5:]
            else:
                return self.data
        return get,
    shortdata = property(*shortdata())

    def niceflags():
        def get(self):
            res = []
            for (f, v) in [
                (self.syn, 'S'), (self.ack, 'A'), (self.fin, 'F'),
                (self.rst, 'R'), (self.stb, 'T')]:
                res.append(f and v or '.')
            return ''.join(res)
        return get,
    niceflags = property(*niceflags())

    def create(cls,
               sourcePseudoPort, destPseudoPort,
               seqNum, ackNum, data,
               window=(1 << 15),
               syn=False, ack=False, fin=False,
               rst=False, stb=False,
               destination=None):
        i = cls(sourcePseudoPort, destPseudoPort,
                seqNum, ackNum, window,
                0, 0, len(data), data)
        i.syn = syn
        i.ack = ack
        i.fin = fin
        i.rst = rst
        i.stb = stb
        i.checksum = i.computeChecksum()
        i.destination = destination
        return i
    create = classmethod(create)


    def __init__(self,
                 sourcePseudoPort,
                 destPseudoPort,
                 seqNum, ackNum, window, flags,
                 checksum, dlen, data, peerAddressTuple=None,
                 seqOffset=0, ackOffset=0, seqLaps=0, ackLaps=0):
        self.sourcePseudoPort = sourcePseudoPort
        self.destPseudoPort = destPseudoPort
        self.seqNum = seqNum
        self.ackNum = ackNum
        self.window = window
        self.flags = flags
        self.checksum = checksum
        self.dlen = dlen
        self.data = data
        self.peerAddressTuple = peerAddressTuple # None if local

        self.seqOffset = seqOffset
        self.ackOffset = ackOffset
        self.seqLaps = seqLaps
        self.ackLaps = ackLaps

    def segmentLength(self):
        """RFC page 26: 'The segment length (SEG.LEN) includes both data and sequence
        space occupying controls'
        """
        return self.dlen + self.syn + self.fin

    def relativeSeq(self):
        return relativeSequence(self.seqNum, self.seqOffset, self.seqLaps)

    def relativeAck(self):
        return relativeSequence(self.ackNum, self.ackOffset, self.ackLaps)


    def verifyChecksum(self):
        if len(self.data) != self.dlen:
            if len(self.data) > self.dlen:
                raise GarbageDataError(self)
            else:
                raise TruncatedDataError(self)
        expected = self.computeChecksum()
        received = self.checksum
        if expected != received:
            raise ChecksumMismatchError(expected, received)

    def computeChecksum(self):
        return crc32(self.data)

    def decode(cls, bytes, hostPortPair):
        fields = struct.unpack(_packetFormat, bytes[:_fixedSize])
        sourcePseudoPort, destPseudoPort, seq, ack, window, flags, checksum, dlen = fields
        data = bytes[_fixedSize:]
        pkt = cls(sourcePseudoPort, destPseudoPort, seq, ack, window, flags,
                  checksum, dlen, data, hostPortPair)
        return pkt
    decode = classmethod(decode)

    def mustRetransmit(self):
        """
        Check to see if this packet must be retransmitted until it was
        received.

        Packets which contain a connection-state changing flag (SYN or FIN) or
        a non-zero amount of data can be retransmitted.
        """
        if self.syn or self.fin or self.dlen:
            return True
        return False

    def encode(self):
        dlen = len(self.data)
        checksum = self.computeChecksum()
        return struct.pack(
            _packetFormat,
            self.sourcePseudoPort, self.destPseudoPort,
            self.seqNum, self.ackNum, self.window,
            self.flags, checksum, dlen) + self.data

    def fragment(self, mtu):
        if self.dlen < mtu:
            return [self]
        assert not self.syn, "should not be originating syn packets w/ data"
        seqOfft = 0
        L = []
        # XXX TODO: need to take seqLaps into account, etc.
        for chunk in iterchunks(self.data, mtu):
            last = self.create(self.sourcePseudoPort,
                               self.destPseudoPort,
                               self.seqNum + seqOfft,
                               self.ackNum,
                               chunk,
                               self.window,
                               destination=self.destination,
                               ack=self.ack)
            L.append(last)
            seqOfft += len(chunk)
        if self.fin:
            last.fin = self.fin
            last.checksum = last.computeChecksum()
        return L


def iterchunks(data, chunksize):
    """iterate chunks of data
    """
    offt = 0
    while offt < len(data):
        yield data[offt:offt+chunksize]
        offt += chunksize


def ISN():
    """
    Initial Sequence Number generator.
    """
    # return int((time.time() * 1000000) / 4) % 2**32
    return 0



def segmentAcceptable(RCV_NXT, RCV_WND, SEG_SEQ, SEG_LEN):
    """
    An acceptable segment: RFC 793 page 26.
    """
    if SEG_LEN == 0 and RCV_WND == 0:
        return SEG_SEQ == RCV_NXT
    if SEG_LEN == 0 and RCV_WND > 0:
        return ((RCV_NXT <= SEG_SEQ) and (SEG_SEQ < RCV_NXT + RCV_WND))
    if SEG_LEN > 0 and RCV_WND == 0:
        return False
    if SEG_LEN > 0 and RCV_WND > 0:
        return ((  (RCV_NXT <= SEG_SEQ) and (SEG_SEQ < RCV_NXT + RCV_WND))
                or ((RCV_NXT <= SEG_SEQ+SEG_LEN-1) and
                    (SEG_SEQ+SEG_LEN-1 < RCV_NXT + RCV_WND)))
    assert 0, 'Should be impossible to get here.'
    return False



def ackAcceptable(SND_UNA, SEG_ACK, SND_NXT):
    """
    An acceptable ACK: RFC 793 page 25.
    """
    return SND_UNA < SEG_ACK <= SND_NXT



class BadPacketError(Exception):
    """
    A packet was bad for some reason.
    """

class ChecksumMismatchError(Exception):
    """
    The checksum and data received did not match.
    """

class TruncatedDataError(Exception):
    """
    The packet was truncated in transit, and all of the data did not arrive.
    """

class GarbageDataError(Exception):
    """
    Too much data was received (???)
    """



class PTCPConnection(object):
    """
    Implementation of RFC 793 state machine.

    @ivar oldestUnackedSendSeqNum: (TCP RFC: SND.UNA) The oldest (relative)
    sequence number referring to an octet which we have sent or may send which
    is unacknowledged.  This begins at 0, which is special because it is not
    for an octet, but rather for the initial SYN packet.  Unless it is 0, this
    represents the sequence number of self._outgoingBytes[0].

    @ivar nextSendSeqNum: (TCP RFC: SND.NXT) The next (relative) sequence
    number that we will send to our peer after the current buffered segments
    have all been acknowledged.  This is the sequence number of the
    not-yet-extant octet in the stream at
    self._outgoingBytes[len(self._outgoingBytes)].

    @ivar nextRecvSeqNum: (TCP RFC: RCV.NXT) The next (relative) sequence
    number that the peer should send to us if they want to send more data;
    their first unacknowledged sequence number as far as we are concerned; the
    left or lower edge of the receive window; the sequence number of the first
    octet that has not been delivered to the application.  changed whenever we
    receive an appropriate ACK.

    @ivar peerSendISN: the initial sequence number that the peer sent us during
    the negotiation phase.  All peer-relative sequence numbers are computed
    using this.  (see C{relativeSequence}).

    @ivar hostSendISN: the initial sequence number that the we sent during the
    negotiation phase.  All host-relative sequence numbers are computed using
    this.  (see C{relativeSequence})

    @ivar retransmissionQueue: a list of packets to be re-sent until their
    acknowledgements come through.

    @ivar recvWindow: (TCP RFC: RCV.WND) - the size [in octets] of the current
    window allowed by this host, to be in transit from the other host.

    @ivar sendWindow: (TCP RFC: SND.WND) - the size [in octets] of the current
    window allowed by our peer, to be in transit from us.

    """

    mtu = 512 - _fixedSize

    recvWindow = mtu
    sendWindow = mtu
    sendWindowRemaining = mtu * 2

    protocol = None

    def __init__(self,
                 hostPseudoPort, peerPseudoPort,
                 ptcp, factory, peerAddressTuple):
        self.hostPseudoPort = hostPseudoPort
        self.peerPseudoPort = peerPseudoPort
        self.ptcp = ptcp
        self.factory = factory
        self._receiveBuffer = []
        self.retransmissionQueue = []
        self.peerAddressTuple = peerAddressTuple

        self.oldestUnackedSendSeqNum = 0
        self.nextSendSeqNum = 0
        self.hostSendISN = 0
        self.nextRecvSeqNum = 0
        self.peerSendISN = 0
        self.setPeerISN = False
        self.machine = TCP(self)

    peerSendISN = None

    def packetReceived(self, packet):
        # XXX TODO: probably have to do something to the packet here to
        # identify its relative sequence number.

        # print 'received', self, packet

        if packet.stb:
            # Shrink the MTU
            [self.mtu] = struct.unpack('!H', packet.data)
            rq = []
            for pkt in self.retransmissionQueue:
                rq.extend(pkt.fragment(self.mtu))
            self.retransmissionQueue = rq
            return

        if self._paused:
            return

        if packet.syn and packet.dlen:
            # Whoops, what?  SYNs probably can contain data, I think, but I
            # certainly don't see anything in the spec about how to deal with
            # this or in ethereal for how linux deals with it -glyph
            raise BadPacketError(
                "currently no data allowed in SYN packets: %r"
                % (packet,))

        if packet.syn:
            assert packet.segmentLength() == 1
            if self.peerAddressTuple is None:
                # we're a server
                assert self.wasEverListen, "Clients must specify a connect address."
                self.peerAddressTuple = packet.peerAddressTuple
            else:
                # we're a client
                assert self.peerAddressTuple == packet.peerAddressTuple
            if self.setPeerISN:
                if self.peerSendISN != packet.seqNum:
                    raise BadPacketError(
                        "Peer ISN was already set to %s but incoming packet "
                        "tried to set it to %s" % (
                            self.peerSendISN, packet.seqNum))
                return
            self.setPeerISN = True
            self.peerSendISN = packet.seqNum
            # syn, fin, and data are mutually exclusive, so this relative
            # sequence-number increment is done both here, and below in the
            # data/fin processing block.
            self.nextRecvSeqNum += packet.segmentLength()
            if not packet.ack:
                # Since "syn" and "synAck" are separate inputs, we produce
                # 'synAck' below once we've ensured the ack is acceptable.
                self.machine.syn()

        if packet.ack and ackAcceptable(self.oldestUnackedSendSeqNum,
                                        packet.relativeAck(),
                                        self.nextSendSeqNum):
            rq = self.retransmissionQueue
            while rq and ((rq[0].relativeSeq() + rq[0].segmentLength())
                          <= packet.relativeAck()):
                # fully acknowledged, as per RFC!
                self.sendWindowRemaining += rq.pop(0).segmentLength()
                # print 'inc send window', self, self.sendWindowRemaining
            self.oldestUnackedSendSeqNum = packet.relativeAck()

            self.machine.maybeReceiveAck(packet)

            if not rq:
                # write buffer is empty; alert the application layer.
                self._writeBufferEmpty()


        # XXX TODO: examine 'window' field and adjust sendWindowRemaining
        # is it 'occupying a portion of valid receive sequence space'?  I think
        # this means 'packet which might acceptably contain useful data'
        if not packet.segmentLength():
            assert packet.ack, "What the _HELL_ is wrong with this packet:" +str(packet)
            return

        if not segmentAcceptable(self.nextRecvSeqNum,
                                 self.recvWindow,
                                 packet.relativeSeq(),
                                 packet.segmentLength()):
            self.ackSoon()
            return

        if packet.relativeSeq() > self.nextRecvSeqNum:
            # XXX: Here's what's going on.  Data can be 'in the window', but
            # still in the future.  For example, if I have a window of length 3
            # and I send segments DATA1(len 1) DATA2(len 1) FIN and you receive
            # them in the order FIN DATA1 DATA2, you don't actually want to
            # process the FIN until you've processed the data.

            # For the moment we are just dropping anything that isn't exactly
            # the next thing we want to process.  This is perfectly valid;
            # these packets might have been dropped, so the other end will have
            # to retransmit them anyway.
            return

        # OK!  It's acceptable!  Let's process the various bits of data.
        # Where is the useful data in the packet?
        if packet.dlen:
            usefulData = packet.data[self.nextRecvSeqNum - packet.relativeSeq():]
            # DONT check/slice the window size here, the acceptability code
            # checked it, we can over-ack if the other side is buggy (???)

            self.machine.segmentReceived()
            if self.protocol is not None:
                try:
                    self.protocol.dataReceived(usefulData)
                except:
                    log.err()
                    self.loseConnection()

        self.nextRecvSeqNum += packet.segmentLength()

        if packet.fin:
            self.machine.fin()
        elif packet.segmentLength() > 0:
            self.ackSoon()


    def getHost(self):
        tupl = self.ptcp.transport.getHost()
        return PTCPAddress((tupl.host, tupl.port),
                           self.pseudoPortPair)

    def getPeer(self):
        return PTCPAddress(self.peerAddressTuple,
                           self.pseudoPortPair)

    _outgoingBytes = ''
    _nagle = None

    def write(self, bytes):
        assert not self.disconnected, 'Writing to a transport that was already disconnected.'
        self._outgoingBytes += bytes
        self._writeLater()


    def writeSequence(self, seq):
        self.write(''.join(seq))


    def _writeLater(self):
        if self._nagle is None:
            self._nagle = reactor.callLater(SEND_DELAY, self._reallyWrite)

    def _originateOneData(self):
        amount = min(self.sendWindowRemaining, self.mtu)
        sendOut = self._outgoingBytes[:amount]
        # print 'originating data packet', len(sendOut)
        self._outgoingBytes = self._outgoingBytes[amount:]
        self.sendWindowRemaining -= len(sendOut)
        self.originate(ack=True, data=sendOut)

    def _reallyWrite(self):
        # print self, 'really writing', self._paused
        self._nagle = None
        if self._outgoingBytes:
            # print 'window and bytes', self.sendWindowRemaining, len(self._outgoingBytes)
            while self.sendWindowRemaining and self._outgoingBytes:
                self._originateOneData()

    _retransmitter = None
    _retransmitTimeout = 0.5

    def _retransmitLater(self):
        if self._retransmitter is None:
            self._retransmitter = reactor.callLater(self._retransmitTimeout,
                                                    self._reallyRetransmit)

    def _stopRetransmitting(self):
        # used both as a quick-and-dirty test shutdown hack and a way to shut
        # down when we die...
        if self._retransmitter is not None:
            self._retransmitter.cancel()
            self._retransmitter = None
        if self._nagle is not None:
            self._nagle.cancel()
            self._nagle = None
        if self._closeWaitLoseConnection is not None:
            self._closeWaitLoseConnection.cancel()
            self._closeWaitLoseConnection = None

    def _reallyRetransmit(self):
        # XXX TODO: packet fragmentation & coalescing.
        # print 'Wee a retransmit!  What I got?', self.retransmissionQueue
        self._retransmitter = None
        if self.retransmissionQueue:
            for packet in self.retransmissionQueue:
                packet.retransmitCount -= 1
                if packet.retransmitCount:
                    packet.ackNum = self.currentAckNum()
                    self.ptcp.sendPacket(packet)
                else:
                    self.machine.timeout()
                    return
            self._retransmitLater()

    disconnecting = False       # This is *TWISTED* level state-machine stuff,
                                # not TCP-level.

    def loseConnection(self):
        if not self.disconnecting:
            self.disconnecting = True
            if not self._outgoingBytes:
                self._writeBufferEmpty()


    def _writeBufferEmpty(self):
        if self._outgoingBytes:
            self._reallyWrite()
        elif self.producer is not None:
            if (not self.streamingProducer) or self.producerPaused:
                self.producerPaused = False
                self.producer.resumeProducing()
        elif self.disconnecting and not self.disconnected:
            self.machine.appClose()


    def _writeBufferFull(self):
        # print 'my write buffer is full'
        if (self.producer is not None
            and not self.producerPaused):
            self.producerPaused = True
            # print 'producer pausing'
            self.producer.pauseProducing()
            # print 'producer paused'
        else:
            # print 'but I am not telling my producer to pause!'
            # print '  ', self.producer, self.streamingProducer, self.producerPaused
            pass


    disconnected = False
    producer = None
    producerPaused = False
    streamingProducer = False

    def registerProducer(self, producer, streaming):
        if self.producer is not None:
            raise RuntimeError(
                "Cannot register producer %s, "
                "because producer %s was never unregistered."
                % (producer, self.producer))
        if self.disconnected:
            producer.stopProducing()
        else:
            self.producer = producer
            self.streamingProducer = streaming
            if not streaming and not self._outgoingBytes:
                producer.resumeProducing()

    def unregisterProducer(self):
        self.producer = None
        if not self._outgoingBytes:
            self._writeBufferEmpty()

    _paused = False
    def pauseProducing(self):
        self._paused = True

    def resumeProducing(self):
        self._paused = False

    def currentAckNum(self):
        return (self.nextRecvSeqNum + self.peerSendISN) % (2**32)

    _ackTimer = None
    def ackSoon(self):
        """
        Emit an acknowledgement packet soon.
        """
        if self._ackTimer is None:
            def originateAck():
                self._ackTimer = None
                self.originate(ack=True)
            self._ackTimer = reactor.callLater(0.1, originateAck)
        else:
            self._ackTimer.reset(ACK_DELAY)

    def originate(self, data='', syn=False, ack=False, fin=False, rst=False):
        """
        Create a packet, enqueue it to be sent, and return it.
        """
        if self._ackTimer is not None:
            self._ackTimer.cancel()
            self._ackTimer = None
        if syn:
            # We really should be randomizing the ISN but until we finish the
            # implementations of the various bits of wraparound logic that were
            # started with relativeSequence
            assert self.nextSendSeqNum == 0, (
                "NSSN = " + repr(self.nextSendSeqNum))
            assert self.hostSendISN == 0
        p = PTCPPacket.create(self.hostPseudoPort,
                              self.peerPseudoPort,
                              seqNum=(self.nextSendSeqNum +
                                      self.hostSendISN) % (2**32),
                              ackNum=self.currentAckNum(),
                              data=data,
                              window=self.recvWindow,
                              syn=syn, ack=ack, fin=fin, rst=rst,
                              destination=self.peerAddressTuple)
        # do we want to enqueue this packet for retransmission?
        sl = p.segmentLength()
        self.nextSendSeqNum += sl

        if p.mustRetransmit():
            # print self, 'originating retransmittable packet', len(self.retransmissionQueue)
            if self.retransmissionQueue:
                if self.retransmissionQueue[-1].fin:
                    raise AssertionError("Sending %r after FIN??!" % (p,))
            # print 'putting it on the queue'
            self.retransmissionQueue.append(p)
            # print 'and sending it later'
            self._retransmitLater()
            if not self.sendWindowRemaining: # len(self.retransmissionQueue) > 5:
                # print 'oh no my queue is too big'
                # This is a random number (5) because I ought to be summing the
                # packet lengths or something.
                self._writeBufferFull()
            else:
                # print 'my queue is still small enough', len(self.retransmissionQueue), self, self.sendWindowRemaining
                pass
        self.ptcp.sendPacket(p)
        return p


    # State machine transition definitions, hooray.
    def outgoingConnectionFailed(self):
        """
        The connection never got anywhere.  Goodbye.
        """
        # XXX CONNECTOR API OMFG
        self.factory.clientConnectionFailed(None, error.TimeoutError())


    wasEverListen = False

    def nowListeningSocket(self):
        # Spec says this is necessary for RST handling; we need it for making
        # sure it's OK to bind port numbers.
        self.wasEverListen = True

    def releaseConnectionResources(self):
        self.ptcp.connectionClosed(self)
        self._stopRetransmitting()
        if self._timeWaitCall is not None:
            self._timeWaitCall.cancel()
            self._timeWaitCall = None
        if self._ackTimer is not None:
            self._ackTimer.cancel()
            self._ackTimer = None

    _timeWaitCall = None
    _timeWaitTimeout = 0.01     # REALLY fast timeout, right now this is for
                                # the tests...

    def scheduleTimeWaitTimeout(self):
        self._stopRetransmitting()
        self._timeWaitCall = reactor.callLater(self._timeWaitTimeout, self._do2mslTimeout)

    def _do2mslTimeout(self):
        self._timeWaitCall = None
        self.machine.timeout()

    peerAddressTuple = None

    def pseudoPortPair():
        def get(self):
            return (self.hostPseudoPort,
                    self.peerPseudoPort)
        return get,
    pseudoPortPair = property(*pseudoPortPair())

    def connectionJustEstablished(self):
        """
        We sent out SYN, they acknowledged it.  Congratulations, you
        have a new baby connection.
        """
        assert not self.disconnecting
        assert not self.disconnected
        try:
            p = self.factory.buildProtocol(PTCPAddress(
                    self.peerAddressTuple, self.pseudoPortPair))
            p.makeConnection(self)
        except:
            log.msg("Exception during PTCP connection setup.")
            log.err()
            self.loseConnection()
        else:
            self.protocol = p

    def connectionJustEnded(self):
        assert not self.disconnected
        self.disconnected = True
        try:
            self.protocol.connectionLost(Failure(CONNECTION_DONE))
        except:
            log.err()
        self.protocol = None

        if self.producer is not None:
            try:
                self.producer.stopProducing()
            except:
                log.err()
            self.producer = None


    _closeWaitLoseConnection = None

    def nowHalfClosed(self):
        # TODO: look for IHalfCloseableProtocol, call the appropriate methods
        def appCloseNow():
            self._closeWaitLoseConnection = None
            self.loseConnection()
        self._closeWaitLoseConnection = reactor.callLater(0.01, appCloseNow)



class PTCPAddress(object):
    # garbage

    def __init__(self, (host, port), (pseudoHostPort, pseudoPeerPort)):
        self.host = host
        self.port = port
        self.pseudoHostPort = pseudoHostPort
        self.pseudoPeerPort = pseudoPeerPort

    def __repr__(self):
        return 'PTCPAddress((%r, %r), (%r, %r))' % (
            self.host, self.port,
            self.pseudoHostPort,
            self.pseudoPeerPort)



class _PendingEvent(object):
    def __init__(self):
        self.listeners = []


    def deferred(self):
        d = Deferred()
        self.listeners.append(d)
        return d


    def callback(self, result):
        l = self.listeners
        self.listeners = []
        for d in l:
            d.callback(result)


    def errback(self, result=None):
        if result is None:
            result = Failure()
        l = self.listeners
        self.listeners = []
        for d in l:
            d.errback(result)



class PTCP(protocol.DatagramProtocol):
    """
    L{PTCP} implements a strongly TCP-like protocol on top of UDP.  It
    provides a transport which is connection-oriented, streaming,
    ordered, and reliable.

    @ivar factory: A L{ServerFactory} which is used to create
        L{IProtocol} providers whenever a new PTCP connection is made
        to this port.

    @ivar _connections: A mapping of endpoint addresses to connection
        objects.  These are the active connections being multiplexed
        over this UDP port.  Many PTCP connections may run over the
        same L{PTCP} instance, communicating with many different
        remote hosts as well as multiplexing different PTCP
        connections to the same remote host.  The mapping keys,
        endpoint addresses, are three-tuples of:

            - The destination pseudo-port which is always C{1}
            - The source pseudo-port
            - A (host, port) tuple giving the UDP address of a PTCP
              peer holding the other side of the connection

        The mapping values, connection objects, are L{PTCPConnection}
        instances.
    @type _connections: C{dict}

    """
    # External API

    def __init__(self, factory):
        self.factory = factory
        self._allConnectionsClosed = _PendingEvent()


    def connect(self, factory, host, port, pseudoPort=1):
        """
        Attempt to establish a new connection via PTCP to the given
        remote address.

        @param factory: A L{ClientFactory} which will be used to
            create an L{IProtocol} provider if the connection is
            successfully set up, or which will have failure callbacks
            invoked on it otherwise.

        @param host: The IP address of another listening PTCP port to
            connect to.
        @type host: C{str}

        @param port: The port number of that other listening PTCP port
            to connect to.
        @type port: C{int}

        @param pseudoPort: Not really implemented.  Do not pass a
            value for this parameter or things will break.

        @return: A L{PTCPConnection} instance representing the new
            connection, but you really shouldn't use this for
            anything.  Write a protocol!
        """
        sourcePseudoPort = genConnID() % MAX_PSEUDO_PORT
        conn = self._connections[(pseudoPort, sourcePseudoPort, (host, port))
                                 ] = PTCPConnection(
            sourcePseudoPort, pseudoPort, self, factory, (host, port))
        conn.machine.appActiveOpen()
        return conn

    def sendPacket(self, packet):
        if self.transportGoneAway:
            return
        self.transport.write(packet.encode(), packet.destination)


    # Internal stuff
    def startProtocol(self):
        self.transportGoneAway = False
        self._lastConnID = 10 # random.randrange(2 ** 32)
        self._connections = {}

    def _finalCleanup(self):
        """
        Clean up all of our connections by issuing application-level close and
        stop notifications, sending hail-mary final FIN packets (which may not
        reach the other end, but nevertheless can be useful) when possible.
        """
        for conn in self._connections.values():
            conn.releaseConnectionResources()
        assert not self._connections

    def stopProtocol(self):
        """
        Notification from twisted that our underlying port has gone away;
        make sure we're not going to try to send any packets through our
        transport and blow up, then shut down all of our protocols, issuing
        appr
        opriate application-level messages.
        """
        self.transportGoneAway = True
        self._finalCleanup()

    def cleanupAndClose(self):
        """
        Clean up all remaining connections, then close our transport.

        Although in a pinch we will do cleanup after our socket has gone away
        (if it does so unexpectedly, above in stopProtocol), we would really
        prefer to do cleanup while we still have access to a transport, since
        that way we can force out a few final packets and save the remote
        application an awkward timeout (if it happens to get through, which
        is generally likely).
        """
        self._finalCleanup()
        return self._stop()

    def datagramReceived(self, bytes, addr):
        if len(bytes) < _fixedSize:
            # It can't be any good.
            return

        pkt = PTCPPacket.decode(bytes, addr)
        try:
            pkt.verifyChecksum()
        except TruncatedDataError:
#             print '(ptcp packet truncated: %r)' % (pkt,)
            self.sendPacket(
                PTCPPacket.create(
                    pkt.destPseudoPort,
                    pkt.sourcePseudoPort,
                    0,
                    0,
                    struct.pack('!H', len(pkt.data)),
                    stb=True,
                    destination=addr))
        except GarbageDataError:
            print("garbage data!", pkt)
        except ChecksumMismatchError, cme:
            print("bad checksum", pkt, cme)
            print(repr(pkt.data))
            print(hex(pkt.checksum), hex(pkt.computeChecksum()))
        else:
            self.packetReceived(pkt)

    stopped = False
    def _stop(self, result=None):
        if not self.stopped:
            self.stopped = True
            return self.transport.stopListening()
        else:
            return defer.succeed(None)

    def waitForAllConnectionsToClose(self):
        """
        Wait for all currently-open connections to enter the 'CLOSED' state.
        Currently this is only usable from test fixtures.
        """
        if not self._connections:
            return self._stop()
        return self._allConnectionsClosed.deferred().addBoth(self._stop)

    def connectionClosed(self, ptcpConn):
        packey = (ptcpConn.peerPseudoPort, ptcpConn.hostPseudoPort,
                  ptcpConn.peerAddressTuple)
        del self._connections[packey]
        if ((not self.transportGoneAway) and
            (not self._connections) and
            self.factory is None):
            self._stop()
        if not self._connections:
            self._allConnectionsClosed.callback(None)

    def packetReceived(self, packet):
        packey = (packet.sourcePseudoPort, packet.destPseudoPort, packet.peerAddressTuple)
        if packey not in self._connections:
            if packet.flags == _SYN and packet.destPseudoPort == 1: # SYN and _ONLY_ SYN set.
                conn = PTCPConnection(packet.destPseudoPort,
                                      packet.sourcePseudoPort, self,
                                      self.factory, packet.peerAddressTuple)
                conn.machine.appPassiveOpen()
                self._connections[packey] = conn
            else:
                log.msg("corrupted packet? %r %r %r" % (packet,packey, self._connections))
                return
        try:
            self._connections[packey].packetReceived(packet)
        except:
            log.msg("PTCPConnection error on %r:" % (packet,))
            log.err()
            del self._connections[packey]
