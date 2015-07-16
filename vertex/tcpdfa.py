# -*- test-case-name: vertex.test.test_ptcp -*-
# Copyright 2005 Divmod, Inc.  See LICENSE file for details

from automat import MethodicalMachine


class TCP(object):
    """
    
    """

    _machine = MethodicalMachine()

    def __init__(self, impl):
        """
        
        """
        self._impl = impl

    @_machine.state(initial=True)
    def closed(self):
        """
        
        """

    # This isn't detailed by the spec in the diagram, so we use a different
    # identifier, but in various places it does make references to going
    # straight to the 'closed' state.
    broken = closed

    @_machine.state()
    def synSent(self):
        """
        
        """

    @_machine.state()
    def synRcvd(self):
        """
        
        """

    @_machine.state()
    def listen(self):
        """
        
        """


    @_machine.state()
    def established(self):
        """
        
        """


    @_machine.state()
    def closeWait(self):
        """
        
        """

    @_machine.state()
    def lastAck(self):
        """
        
        """

    @_machine.state()
    def finWait1(self):
        """
        
        """
    @_machine.state()
    def finWait2(self):
        """
        
        """

    @_machine.state()
    def closing(self):
        """
        
        """

    @_machine.state()
    def timeWait(self):
        """
        
        """

    @_machine.input()
    def appPassiveOpen(self):
        """
        
        """
        

    @_machine.input()
    def appActiveOpen(self):
        """
        
        """

    @_machine.input()
    def timeout(self):
        """
        
        """

    @_machine.input()
    def appClose(self):
        """
        
        """

    @_machine.input()
    def synAck(self):
        """
        
        """

    @_machine.input()
    def ack(self):
        """
        
        """

    @_machine.input()
    def rst(self):
        """
        
        """
        

    @_machine.input()
    def appSendData(self):
        """
        
        """
        

    @_machine.input()
    def syn(self):
        """
        
        """

    @_machine.input()
    def fin(self):
        """
        
        """


    @_machine.input()
    def segmentReceived(self):
        """
        Bonus input!  This is when the segment length of an incoming packet is
        non-zero; in other words, some data has arrived, probably (hopefully?)
        in ESTABLISHED, and we have to send an acknowledgement.
        """


    @_machine.output()
    def sendSyn(self):
        """
        
        """
        self._impl.originate(syn=True)


    @_machine.output()
    def sendFin(self):
        """
        
        """
        self._impl.originate(fin=True)


    @_machine.output()
    def sendSynAck(self):
        """
        
        """
        self._impl.originate(syn=True, ack=True)


    @_machine.output()
    def sendAck(self):
        """
        
        """
        self._impl.originate(ack=True)

    @_machine.output()
    def sendRst(self):
        """
        
        """
        # note: unused / undefined in original impl, need test
        self._impl.originate(rst=True)


    @_machine.output()
    def appNotifyConnected(self):
        """
        
        """
        self._impl.connectionJustEstablished()


    @_machine.output()
    def appNotifyDisconnected(self):
        """
        
        """
        self._impl.connectionJustEnded()


    @_machine.output()
    def releaseResources(self):
        """
        
        """
        self._impl.releaseConnectionResources()


    @_machine.output()
    def startTimeWaiting(self):
        """
        
        """
        self._impl.scheduleTimeWaitTimeout()

    @_machine.output()
    def appNotifyListen(self):
        """
        
        """
        self._impl.nowListeningSocket()

    @_machine.output()
    def appNotifyHalfClose(self):
        """
        Input ended.
        """
        self._impl.nowHalfClosed()


    @_machine.output()
    def appNotifyAttemptFailed(self):
        """
        
        """
        self._impl.outgoingConnectionFailed()


    closed.upon(appPassiveOpen, enter=listen, outputs=[appNotifyListen])
    closed.upon(appActiveOpen, enter=synSent, outputs=[sendSyn])

    synSent.upon(timeout, enter=closed,
                 outputs=[appNotifyAttemptFailed, releaseResources])
    synSent.upon(appClose, enter=closed,
                 outputs=[appNotifyAttemptFailed, releaseResources])
    synSent.upon(synAck, enter=established, outputs=[sendAck,
                                                     appNotifyConnected])

    synRcvd.upon(ack, enter=established, outputs=[appNotifyConnected])
    synRcvd.upon(appClose, enter=finWait1, outputs=[sendFin])
    synRcvd.upon(timeout, enter=closed, outputs=[sendRst, releaseResources])
    synRcvd.upon(rst, enter=broken,
                 outputs=[releaseResources])

    listen.upon(appSendData, enter=synSent, outputs=[sendSyn])
    listen.upon(syn, enter=synRcvd, outputs=[sendSynAck])

    established.upon(segmentReceived, enter=established,
                     outputs=[sendAck])

    established.upon(appClose, enter=finWait1,
                     outputs=[appNotifyDisconnected,
                              sendFin])
    established.upon(fin, enter=closeWait,
                     outputs=[appNotifyHalfClose,
                              sendAck])
    established.upon(timeout, enter=broken, outputs=[appNotifyDisconnected,
                                                     releaseResources])

    closeWait.upon(appClose, enter=lastAck,
                   outputs=[sendFin,
                            appNotifyDisconnected])
    closeWait.upon(timeout, enter=broken,
                   outputs=[appNotifyDisconnected,
                            releaseResources])

    lastAck.upon(ack, enter=closed, outputs=[releaseResources])
    lastAck.upon(timeout, enter=broken, outputs=[releaseResources])

    finWait1.upon(ack, enter=finWait2, outputs=[])
    finWait1.upon(fin, enter=closing, outputs=[sendAck])
    finWait1.upon(timeout, enter=broken, outputs=[releaseResources])

    finWait2.upon(timeout, enter=broken, outputs=[releaseResources])
    finWait2.upon(fin, enter=timeWait, outputs=[sendAck, startTimeWaiting])

    closing.upon(timeout, enter=broken, outputs=[releaseResources])
    closing.upon(ack, enter=timeWait, outputs=[startTimeWaiting])

    timeWait.upon(timeout, enter=closed, outputs=[releaseResources])

    for noDataState in [finWait1, finWait2, closing]:
        noDataState.upon(segmentReceived, enter=noDataState, outputs=[])
