#!/usr/bin/env python
#-*- coding: utf-8 -*-

from array import array

import nose
from nose.tools import eq_, ok_, raises

from pyipmi.interfaces.ipmb import checksum, IpmbHeader

def test_checksum():
    eq_(checksum([1,2,3,4,5]), 256-15)

def test_encode():
    header = IpmbHeader()
    header.rs_lun = 0
    header.rs_sa = 0x72
    header.rq_seq = 2
    header.rq_lun = 0
    header.rq_sa = 0x20
    header.netfn = 6
    header.cmd_id = 1
    data = header.encode()
    eq_(data, array('B', [0x72, 0x18, 0x76, 0x20, 0x08, 0x01]))
