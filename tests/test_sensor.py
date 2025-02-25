#!/usr/bin/env python
# -*- coding: utf-8 -*-

from nose.tools import eq_
from mock import MagicMock

from pyipmi import interfaces, create_connection
from pyipmi.msgs.sensor import SetSensorThresholdsRsp, PlatformEventRsp
from pyipmi.sensor import (EVENT_READING_TYPE_SENSOR_SPECIFIC,
                           SENSOR_TYPE_MODULE_HOT_SWAP)


def test_set_sensor_thresholds():

    rsp = SetSensorThresholdsRsp()
    rsp.completion_code = 0

    mock_send_recv = MagicMock()
    mock_send_recv.return_value = rsp

    interface = interfaces.create_interface('mock')
    ipmi = create_connection(interface)
    ipmi.send_message = mock_send_recv

    ipmi.set_sensor_thresholds(sensor_number=5, lun=1)
    args, _ = mock_send_recv.call_args
    req = args[0]
    eq_(req.lun, 1)
    eq_(req.sensor_number, 5)

    ipmi.set_sensor_thresholds(sensor_number=0, unr=10)
    args, _ = mock_send_recv.call_args
    req = args[0]
    eq_(req.set_mask.unr, 1)
    eq_(req.threshold.unr, 10)
    eq_(req.set_mask.ucr, 0)
    eq_(req.threshold.ucr, 0)
    eq_(req.set_mask.unc, 0)
    eq_(req.threshold.unc, 0)
    eq_(req.set_mask.lnc, 0)
    eq_(req.threshold.lnc, 0)
    eq_(req.set_mask.lcr, 0)
    eq_(req.threshold.lcr, 0)
    eq_(req.set_mask.lnr, 0)
    eq_(req.threshold.lnr, 0)

    ipmi.set_sensor_thresholds(sensor_number=5, ucr=11)
    args, _ = mock_send_recv.call_args
    req = args[0]
    eq_(req.lun, 0)
    eq_(req.set_mask.unr, 0)
    eq_(req.threshold.unr, 0)
    eq_(req.set_mask.ucr, 1)
    eq_(req.threshold.ucr, 11)
    eq_(req.set_mask.unc, 0)
    eq_(req.threshold.unc, 0)
    eq_(req.set_mask.lnc, 0)
    eq_(req.threshold.lnc, 0)
    eq_(req.set_mask.lcr, 0)
    eq_(req.threshold.lcr, 0)
    eq_(req.set_mask.lnr, 0)
    eq_(req.threshold.lnr, 0)


def test_send_platform_event():

    rsp = PlatformEventRsp()
    rsp.completion_code = 0

    mock_send_recv = MagicMock()
    mock_send_recv.return_value = rsp

    interface = interfaces.create_interface('mock')
    ipmi = create_connection(interface)
    ipmi.send_message = mock_send_recv

    # Module handle closed event
    ipmi.send_platform_event(SENSOR_TYPE_MODULE_HOT_SWAP, 1,
                             EVENT_READING_TYPE_SENSOR_SPECIFIC, asserted=True,
                             event_data=[0, 0xff, 0xff])
    args, _ = mock_send_recv.call_args
    req = args[0]
    eq_(req.event_message_rev, 4)
    eq_(req.sensor_type, 0xf2)
    eq_(req.sensor_number, 1)
    eq_(req.event_type.type, 0x6f)
    eq_(req.event_type.dir, 0)
    eq_(req.event_data, [0, 0xff, 0xff])
