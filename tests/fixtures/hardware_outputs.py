"""Real hardware detection output samples for use in parametrized tests.

Each constant is a verbatim copy of output seen on real hardware.
Adding a new platform variant? Paste the raw output here and add a
parametrize case in test_hardware_parsing.py.
"""

# --- libcamera-hello --list-cameras ---

LIBCAMERA_RPI5_SINGLE = """\
Available cameras
-----------------
0 : imx708 [4608x2592 10-bit RGGB] (/base/axi/pcie@120000/rp1/i2c@88000/imx708@1a)
"""

LIBCAMERA_RPI5_WITH_WARNINGS = """\
WARNING: ControlValidator: Control 0x009e0902 missing from controls
WARNING: ControlValidator: Control 0x009e0903 missing from controls
Available cameras
-----------------
0 : imx708 [4608x2592 10-bit RGGB] (/base/axi/pcie@120000/rp1/i2c@88000/imx708@1a)
"""

LIBCAMERA_MULTIPLE_CAMERAS = """\
Available cameras
-----------------
0 : imx708 [4608x2592 10-bit RGGB] (/base/.../imx708@1a)
1 : ov5647 [2592x1944 10-bit BAYER] (/base/.../ov5647@36)
"""

LIBCAMERA_EMPTY = """\
Available cameras
-----------------
No cameras available!
"""

# --- arecord -l ---

ARECORD_WITH_USB_MIC = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

ARECORD_WITH_PI_MIC = """\
**** List of CAPTURE Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
  Subdevices: 8/8
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""

ARECORD_SINGLE_BUILTIN = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
  Subdevices: 1/1
"""

ARECORD_EMPTY = """\
**** List of CAPTURE Hardware Devices ****
"""

# --- v4l2-ctl --list-devices ---

V4L2CTL_SINGLE_USB = """\
USB Camera (usb-0000:00:14.0-2):
\t/dev/video0
\t/dev/video1

"""

V4L2CTL_MULTIPLE_DEVICES = """\
USB Camera (usb-0000:00:14.0-2):
\t/dev/video0
\t/dev/video1

HD Video Capture (usb-0000:00:14.0-3):
\t/dev/video2

"""

V4L2CTL_PI_USB_CAMERA = """\
USB Camera: USB Camera (usb-0000:01:00.0-1.4):
\t/dev/video0
\t/dev/video1

bcm2835-codec-decode (platform:bcm2835-codec):
\t/dev/video10
\t/dev/video11

"""

V4L2CTL_EMPTY = ""
