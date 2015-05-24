"""
 mbed CMSIS-DAP debugger
 Copyright (c) 2006-2015 ARM Limited

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

from pyOCD.target.target import TARGET_RUNNING
import logging
from struct import unpack
from time import time
from flash_builder import FLASH_PAGE_ERASE, FLASH_CHIP_ERASE, FlashBuilder

DEFAULT_PAGE_PROGRAM_WEIGHT = 0.130
DEFAULT_PAGE_ERASE_WEIGHT   = 0.048
DEFAULT_CHIP_ERASE_WEIGHT   = 0.174

# Program to compute the CRC of sectors.  This works on cortex-m processors.
# Code is relocatable and only needs to be on a 4 byte boundary.
# 200 bytes of executable data below + 1024 byte crc table = 1224 bytes
# Usage requirements:
# -In memory reserve 0x600 for code & table
# -Make sure data buffer is big enough to hold 4 bytes for each page that could be checked (ie.  >= num pages * 4)
analyzer = (
    0x2180468c, 0x2600b5f0, 0x4f2c2501, 0x447f4c2c, 0x1c2b0049, 0x425b4033, 0x40230872, 0x085a4053,
    0x425b402b, 0x40534023, 0x402b085a, 0x4023425b, 0x085a4053, 0x425b402b, 0x40534023, 0x402b085a,
    0x4023425b, 0x085a4053, 0x425b402b, 0x40534023, 0x402b085a, 0x4023425b, 0x085a4053, 0x425b402b,
    0x40534023, 0xc7083601, 0xd1d2428e, 0x2b004663, 0x4663d01f, 0x46b4009e, 0x24ff2701, 0x44844d11,
    0x1c3a447d, 0x88418803, 0x4351409a, 0xd0122a00, 0x22011856, 0x780b4252, 0x40533101, 0x009b4023,
    0x0a12595b, 0x42b1405a, 0x43d2d1f5, 0x4560c004, 0x2000d1e7, 0x2200bdf0, 0x46c0e7f8, 0x000000b6,
    0xedb88320, 0x00000044,
    )

def _msb( n ):
    ndx = 0
    while ( 1 < n ):
        n = ( n >> 1 )
        ndx += 1
    return ndx

def _same(d1, d2):
    if len(d1) != len(d2):
        return False
    for i in range(len(d1)):
        if d1[i] != d2[i]:
            return False
    return True

class PageInfo(object):

    def __init__(self):
        self.erase_weight = None        # Time it takes to erase a page
        self.program_weight = None      # Time it takes to program a page (Not including data transfer time)
        self.size = None                # Size of page
        self.crc_supported = None       # Is the function computeCrcs supported?

class FlashInfo(object):

    def __init__(self):
        self.rom_start = None           # Starting address of ROM
        self.erase_weight = None        # Time it takes to perform a chip erase

class Flash(object):
    """
    This class is responsible to flash a new binary in a target
    """

    def __init__(self, target, flash_algo):
        self.target = target
        self.flash_algo = flash_algo
        self.flash_algo_debug = False
        if flash_algo is not None:
            self.end_flash_algo = flash_algo['load_address'] + len(flash_algo)*4
            self.begin_stack = flash_algo['begin_stack']
            self.begin_data = flash_algo['begin_data']
            self.static_base = flash_algo['static_base']
            self.page_size = flash_algo['page_size']
        else:
            self.end_flash_algo = None
            self.begin_stack = None
            self.begin_data = None
            self.static_base = None
            self.page_size = None

    def init(self):
        """
        Download the flash algorithm in RAM
        """
        self.target.halt()
        self.target.setTargetState("PROGRAM")

        # update core register to execute the init subroutine
        result = self.callFunction(self.flash_algo['pc_init'], init=True)

        # check the return code
        if result != 0:
            logging.error('init error: %i', result)

        return

    def computeCrcs(self, sectors):

        data = []

        # Convert address, size pairs into commands
        # for the crc computation algorithm to preform
        for addr, size in sectors:
            size_val = _msb(size)
            addr_val = addr // size
            # Size must be a power of 2
            assert (1 << size_val) == size
            # Address must be a multiple of size
            assert (addr % size) == 0
            val = (size_val << 0) | (addr_val << 16)
            data.append(val)

        self.target.writeBlockMemoryAligned32(self.begin_data, data)

        # update core register to execute the subroutine
        result = self.callFunction(self.flash_algo['analyzer_address'], self.begin_data, len(data))

        # Read back the CRCs for each section
        data = self.target.readBlockMemoryAligned32(self.begin_data, len(data))
        return data

    def eraseAll(self):
        """
        Erase all the flash
        """

        # update core register to execute the eraseAll subroutine
        result = self.callFunction(self.flash_algo['pc_eraseAll'])

        # check the return code
        if result != 0:
            logging.error('eraseAll error: %i', result)

        return

    def erasePage(self, flashPtr):
        """
        Erase one page
        """

        # update core register to execute the erasePage subroutine
        result = self.callFunction(self.flash_algo['pc_erase_sector'], flashPtr)

        # check the return code
        if result != 0:
            logging.error('erasePage(0x%x) error: %i', flashPtr, result)

        return

    def programPage(self, flashPtr, bytes):
        """
        Flash one page
        """

        # prevent security settings from locking the device
        bytes = self.overrideSecurityBits(flashPtr, bytes)

        # first transfer in RAM
        self.target.writeBlockMemoryUnaligned8(self.begin_data, bytes)

        # update core register to execute the program_page subroutine
        result = self.callFunction(self.flash_algo['pc_program_page'], flashPtr, self.page_size, self.begin_data)

        # check the return code
        if result != 0:
            logging.error('programPage(0x%x) error: %i', flashPtr, result)

        return

    def getPageInfo(self, addr):
        """
        Get info about the page that contains this address

        Override this function if variable page sizes are supported
        """
        info = PageInfo()
        info.erase_weight = DEFAULT_PAGE_ERASE_WEIGHT
        info.program_weight = DEFAULT_PAGE_PROGRAM_WEIGHT
        info.size = self.flash_algo['page_size']
        return info

    def getFlashInfo(self):
        """
        Get info about the flash

        Override this function to return differnt values
        """
        info = FlashInfo()
        info.rom_start = 0
        info.erase_weight = DEFAULT_CHIP_ERASE_WEIGHT
        info.crc_supported = self.flash_algo['analyzer_supported']
        return info

    def getFlashBuilder(self):
        return FlashBuilder(self, self.getFlashInfo().rom_start)

    def flashBlock(self, addr, data, smart_flash = True, chip_erase = None, progress_cb = None):
        """
        Flash a block of data
        """
        flash_start = self.getFlashInfo().rom_start
        fb = FlashBuilder(self, flash_start)
        fb.addData(addr, data)
        info = fb.program(chip_erase, progress_cb, smart_flash)
        return info

    def flashBinary(self, path_file, flashPtr = 0x0000000, smart_flash = True, chip_erase = None, progress_cb = None):
        """
        Flash a binary
        """
        f = open(path_file, "rb")

        with open(path_file, "rb") as f:
            data = f.read()
        data = unpack(str(len(data)) + 'B', data)
        self.flashBlock(flashPtr, data, smart_flash, chip_erase, progress_cb)

    def callFunction(self, pc, r0=None, r1=None, r2=None, r3=None, init=False):
        reg_list = []
        data_list = []

        if self.flash_algo_debug:
            vector_catch_enabled = self.target.getVectorCatchFault()
            reset_catch_enabled = self.target.getVectorCatchReset()
            self.target.setVectorCatchFault(True)
            self.target.setVectorCatchReset(True)

        if init:
            # download flash algo in RAM
            self.target.writeBlockMemoryAligned32(self.flash_algo['load_address'], self.flash_algo['instructions'])
            if self.flash_algo['analyzer_supported']:
                self.target.writeBlockMemoryAligned32(self.flash_algo['analyzer_address'], analyzer)

        reg_list.append('pc')
        data_list.append(pc)
        if r0 is not None:
            reg_list.append('r0')
            data_list.append(r0)
        if r1 is not None:
            reg_list.append('r1')
            data_list.append(r1)
        if r2 is not None:
            reg_list.append('r2')
            data_list.append(r2)
        if r3 is not None:
            reg_list.append('r3')
            data_list.append(r3)
        if init:
            reg_list.append('r9')
            data_list.append(self.static_base)
        if init:
            reg_list.append('sp')
            data_list.append(self.begin_stack)
        reg_list.append('lr')
        data_list.append(self.flash_algo['load_address'] + 1)
        self.target.writeCoreRegistersRaw(reg_list, data_list)

        # resume and wait until the breakpoint is hit
        self.target.resume()
        while(self.target.getState() == TARGET_RUNNING):
            pass

        result = self.target.readCoreRegister('r0')

        if self.flash_algo_debug:
            analyzer_supported = self.flash_algo['analyzer_supported']

            expected_fp = self.flash_algo['static_base']
            expected_sp = self.flash_algo['begin_stack']
            expected_pc = self.flash_algo['load_address']
            expected_flash_algo = self.flash_algo['instructions']
            if analyzer_supported:
                expected_analyzer = analyzer
            final_fp = self.target.readCoreRegister('r9')
            final_sp = self.target.readCoreRegister('sp')
            final_pc = self.target.readCoreRegister('pc')
            #TODO - uncomment if Read/write and zero init sections can be moved into a separate flash algo section
            #final_flash_algo = self.target.readBlockMemoryAligned32(self.flash_algo['load_address'], len(self.flash_algo['instructions']))
            #if analyzer_supported:
            #    final_analyzer = self.target.readBlockMemoryAligned32(self.flash_algo['analyzer_address'], len(analyzer))

            error = False
            if final_fp != expected_fp:
                # Frame pointer should not change
                logging.error("Frame pointer should be 0x%x but is 0x%x" % (expected_fp, final_fp))
                error = True
            if final_sp != expected_sp:
                # Stack pointer should return to original value after function call
                logging.error("Stack pointer should be 0x%x but is 0x%x" % (expected_sp, final_sp))
                error = True
            if final_pc != expected_pc:
                # PC should be pointing to breakpoint address
                logging.error("PC should be 0x%x but is 0x%x" % (expected_pc, final_pc))
                error = True
            #TODO - uncomment if Read/write and zero init sections can be moved into a separate flash algo section
            #if not _same(expected_flash_algo, final_flash_algo):
            #    logging.error("Flash algorithm overwritten!")
            #    error = True
            #if analyzer_supported and not _same(expected_analyzer, final_analyzer):
            #    logging.error("Analyzer overwritten!")
            #    error = True
            assert error == False
            self.target.setVectorCatchFault(vector_catch_enabled)
            self.target.setVectorCatchReset(reset_catch_enabled)

        return result

    def setFlashAlgoDebug(self, enable):
        """
        Turn on extra flash algorithm checking

        When set this will greatly slow down flash algo performance
        """
        self.flash_algo_debug = enable

    def overrideSecurityBits(self, address, data):
        return data
