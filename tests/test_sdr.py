#!/usr/bin/env python
# -*- coding: utf-8 -*-

from nose.tools import eq_, ok_, raises

from pyipmi.errors import DecodingError
from pyipmi.sdr import (SdrCommon, SdrFullSensorRecord, SdrCompactSensorRecord,
                        SdrEventOnlySensorRecord, SdrFruDeviceLocator,
                        SdrManagementControllerDeviceLocator,
                        SdrManagementControllerConfirmationRecord,
                        SdrUnknownSensorRecord)


class TestSdrFullSensorRecord():
    def test_convert_complement(self):
        eq_(SdrFullSensorRecord()._convert_complement(0x8, 4), -8)
        eq_(SdrFullSensorRecord()._convert_complement(0x80, 8), -128)
        eq_(SdrFullSensorRecord()._convert_complement(0x8000, 16), -32768)

    def test_decode_capabilities(self):
        record = SdrFullSensorRecord()

        record._decode_capabilities(0)
        ok_('ignore_sensor' not in record.capabilities)
        ok_('auto_rearm' not in record.capabilities)
        ok_('hysteresis_not_supported' in record.capabilities)
        ok_('threshold_not_supported' in record.capabilities)

        record._decode_capabilities(0x80)
        ok_('ignore_sensor' in record.capabilities)
        ok_('auto_rearm' not in record.capabilities)
        ok_('hysteresis_not_supported' in record.capabilities)
        ok_('threshold_not_supported' in record.capabilities)

        record._decode_capabilities(0x40)
        ok_('ignore_sensor' not in record.capabilities)
        ok_('auto_rearm' in record.capabilities)
        ok_('hysteresis_not_supported' in record.capabilities)
        ok_('threshold_not_supported' in record.capabilities)

        record._decode_capabilities(0x30)
        ok_('ignore_sensor' not in record.capabilities)
        ok_('auto_rearm' not in record.capabilities)
        ok_('hysteresis_fixed' in record.capabilities)
        ok_('threshold_not_supported' in record.capabilities)

        record._decode_capabilities(0x0c)
        ok_('ignore_sensor' not in record.capabilities)
        ok_('auto_rearm' not in record.capabilities)
        ok_('hysteresis_not_supported' in record.capabilities)
        ok_('threshold_fixed' in record.capabilities)

    @raises(DecodingError)
    def test_invalid_length(self):
        data = (0, 0, 0, 0, 0)
        SdrFullSensorRecord(data)

    @raises(DecodingError)
    def test_linearization_key_error(self):
        sdr = SdrFullSensorRecord(None)
        sdr.linearization = 12
        sdr.l(1)

    def test_linearization(self):
        sdr = SdrFullSensorRecord(None)

        # linear
        sdr.linearization = 0
        eq_(sdr.l(1), 1)
        eq_(sdr.l(10), 10)

        # ln
        sdr.linearization = 1
        eq_(sdr.l(1), 0)

        # log
        sdr.linearization = 2
        eq_(sdr.l(10), 1)
        eq_(sdr.l(100), 2)

        # log
        sdr.linearization = 3
        eq_(sdr.l(8), 3)
        eq_(sdr.l(16), 4)

        # e
        sdr.linearization = 4
        eq_(sdr.l(1), 2.718281828459045)

        # exp10
        sdr.linearization = 5
        eq_(sdr.l(1), 10)
        eq_(sdr.l(2), 100)

        # exp2
        sdr.linearization = 6
        eq_(sdr.l(3), 8)
        eq_(sdr.l(4), 16)

        # 1/x
        sdr.linearization = 7
        eq_(sdr.l(2), 0.5)
        eq_(sdr.l(4), 0.25)

        # sqr
        sdr.linearization = 8
        eq_(sdr.l(2), 4)

        # cube
        sdr.linearization = 9
        eq_(sdr.l(2), 8)
        eq_(sdr.l(3), 27)

        # sqrt
        sdr.linearization = 10
        eq_(sdr.l(16), 4)

        # cubert
        sdr.linearization = 11
        eq_(sdr.l(8), 2)
        eq_(sdr.l(27), 3)

    def test_convert_sensor_raw_to_value(self):
        sdr = SdrFullSensorRecord()
        eq_(sdr.convert_sensor_raw_to_value(None), None)

        sdr.analog_data_format = sdr.DATA_FMT_UNSIGNED
        sdr.m = 1
        sdr.b = 0
        sdr.k1 = 0
        sdr.k2 = 0
        sdr.linearization = 0
        eq_(sdr.convert_sensor_raw_to_value(1), 1)
        eq_(sdr.convert_sensor_raw_to_value(255), 255)

        sdr.analog_data_format = sdr.DATA_FMT_UNSIGNED
        sdr.m = 10
        sdr.b = 0
        sdr.k1 = 0
        sdr.k2 = 0
        sdr.linearization = 0
        eq_(sdr.convert_sensor_raw_to_value(1), 10)
        eq_(sdr.convert_sensor_raw_to_value(255), 2550)

        sdr.analog_data_format = sdr.DATA_FMT_1S_COMPLEMENT
        sdr.m = 1
        sdr.b = 0
        sdr.k1 = 0
        sdr.k2 = 0
        sdr.linearization = 0
        eq_(sdr.convert_sensor_raw_to_value(1), 1)
        eq_(sdr.convert_sensor_raw_to_value(128), -127)
        eq_(sdr.convert_sensor_raw_to_value(255), 0)

        sdr.analog_data_format = sdr.DATA_FMT_2S_COMPLEMENT
        sdr.m = 1
        sdr.b = 0
        sdr.k1 = 0
        sdr.k2 = 0
        sdr.linearization = 0
        eq_(sdr.convert_sensor_raw_to_value(1), 1)
        eq_(sdr.convert_sensor_raw_to_value(128), -128)
        eq_(sdr.convert_sensor_raw_to_value(255), -1)

    def test_decocde(self):
        data = [0x17, 0x00, 0x51, 0x01, 0x35, 0x17, 0x00, 0x51,
                0x01, 0x35, 0x17, 0x00, 0x51, 0x01, 0x35, 0x32,
                0x85, 0x32, 0x1b, 0x1b, 0x00, 0x04, 0x00, 0x00,
                0x3b, 0x01, 0x00, 0x01, 0x00, 0xd0, 0x07, 0xcc,
                0xf4, 0xa6, 0xff, 0x00, 0x00, 0xfe, 0xf5, 0x00,
                0x8e, 0xa5, 0x04, 0x04, 0x00, 0x00, 0x00, 0xca,
                0x41, 0x32, 0x3a, 0x56, 0x63, 0x63, 0x20, 0x31,
                0x32, 0x56]
        sdr = SdrCommon.from_data(data)
        ok_(isinstance(sdr, SdrFullSensorRecord))
        eq_(str(sdr), '["b\'A2:Vcc 12V\'   "] [1:53] [17 00 51 01 35 17 00 '
                      '51 01 35 17 00 51 01 35 32 85 32 1b 1b 00 04 00 00 '
                      '3b 01 00 01 00 d0 07 cc f4 a6 ff 00 00 fe f5 00 8e '
                      'a5 04 04 00 00 00 ca 41 32 3a 56 63 63 20 31 32 56]')
        eq_(sdr.device_id_string, b'A2:Vcc 12V')


class TestSdrCommon():
    @raises(DecodingError)
    def test_invalid_data_length(self):
        data = (0x00, 0x01, 0x02, 0x03)
        SdrCommon(data)

    def test_object(self):
        data = (0x00, 0x01, 0x02, 0x03, 0x04)
        sdr = SdrCommon(data)
        eq_(sdr.id, 0x0100)
        eq_(sdr.version, 0x02)
        eq_(sdr.type, 0x03)
        eq_(sdr.length, 0x04)


class TestSdrCompactSensorRecord():

    @raises(DecodingError)
    def test_invalid_length(self):
        data = (0, 0, 0, 0, 0)
        SdrCompactSensorRecord(data)

    def test_decode(self):
        data = [0xd3, 0x00, 0x51, 0x02, 0x28, 0x82, 0x00, 0xd3,
                0xc1, 0x64, 0x03, 0x40, 0x21, 0x6f, 0x00, 0x00,
                0x00, 0x00, 0x03, 0x00, 0xc0, 0x00, 0x00, 0x01,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xcd,
                0x41, 0x34, 0x3a, 0x50, 0x72, 0x65, 0x73, 0x20,
                0x53, 0x46, 0x50, 0x2d, 0x31]
        sdr = SdrCommon.from_data(data)
        ok_(isinstance(sdr, SdrCompactSensorRecord))
        eq_(str(sdr), '["b\'A4:Pres SFP-1\'"] [d3 00 51 02 28 82 00 d3 c1 64 '
                       '03 40 21 6f 00 00 00 00 03 00 c0 00 00 01 00 00 00 '
                       '00 00 00 00 cd 41 34 3a 50 72 65 73 20 53 46 50 2d 31]')


@raises(DecodingError)
def test_sdreventonlysensorrecord():
    data = (0, 0, 0, 0, 0)
    SdrEventOnlySensorRecord(data)


class TestSdrFruDeviceLocatorRecord():

    @raises(DecodingError)
    def test_invalid_length(self):
        data = (0, 0, 0, 0, 0)
        SdrFruDeviceLocator(data)

    def test_decode(self):
        data = [0x02, 0x00, 0x51, 0x11, 0x17, 0x82, 0x03, 0x80,
                0x00, 0x00, 0x10, 0x02, 0xc2, 0x61, 0x00, 0xcc,
                0x4b, 0x6f, 0x6e, 0x74, 0x72, 0x6f, 0x6e, 0x20,
                0x4d, 0x43, 0x4d, 0x43]
        sdr = SdrCommon.from_data(data)
        ok_(isinstance(sdr, SdrFruDeviceLocator))
        eq_(str(sdr), '["b\'Kontron MCMC\' "] [02 00 51 11 17 82 03 80 00 00 '
                      '10 02 c2 61 00 cc 4b 6f 6e 74 72 6f 6e 20 4d 43 4d 43]')


class TestSdrManagementControllerDeviceRecord():

    @raises(DecodingError)
    def test_invalid_length(self):
        data = (0, 0, 0, 0, 0)
        SdrManagementControllerDeviceLocator(data)

    def test_decode(self):
        data = [0x00, 0x01, 0x51, 0x12, 0x1b, 0x00, 0x01, 0x51,
                0x12, 0x1b, 0x00, 0x01, 0x51, 0x12, 0x1b, 0xd0,
                0x41, 0x32, 0x3a, 0x41, 0x4d, 0x34, 0x32, 0x32,
                0x30, 0x20]
        sdr = SdrCommon.from_data(data)
        ok_(isinstance(sdr, SdrManagementControllerDeviceLocator))
        eq_(str(sdr), '["b\'A2:AM4220 \'   "] [00 01 51 12 1b 00 01 51 12 1b '
                      '00 01 51 12 1b d0 41 32 3a 41 4d 34 32 32 30 20]')
        eq_(sdr.device_id_string, b'A2:AM4220 ')


class TestSdrManagementControllerConfirmationRecord():
        data = [0x45, 0x00, 0x51, 0x13, 0x1b, 0x20, 0x00, 0x01,
                0x02, 0x01, 0x51, 0x4a, 0xc1, 0x62, 0x06, 0x80,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        sdr = SdrCommon.from_data(data)
        ok_(isinstance(sdr, SdrManagementControllerConfirmationRecord))
        eq_(str(sdr), '[45 00 51 13 1b 20 00 01 02 01 51 4a c1 62 06 80 00 '
                      '00 00 00 00 00 00 00 00 00 00 00 00 00 00 00]')
        eq_(sdr.ipmi_version, 0x51)
        eq_(sdr.manufacturer_id, 0x2c14a)
        eq_(sdr.product_id, 0x8006)


def test_unknown_record_type():
    data = [0x01, 0x0, 0x51, 0x0a, 0x0]
    sdr = SdrCommon.from_data(data, 0xffff)
    ok_(isinstance(sdr, SdrUnknownSensorRecord))
