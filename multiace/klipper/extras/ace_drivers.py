# Driver registry: the codecs multiACE may use to discover and talk to
# units, and the config switch that turns each of them on or off.
#
# A driver is a codec class (AceProtocol subclass) plus its [ace] option.
# Discovery only runs the enabled drivers: a disabled driver probes no
# port and connects no unit. Every option is read unconditionally
# (Klipper halts on an option that is present but never read).
#
# A new driver arrives as a pull request: add its codec module, append one
# Driver entry here, ship its option commented in config/extended/ace.cfg
# and add the module to the packing lists (install/uninstall scripts,
# post-commit hook, bake list, release workflow, managed manifest,
# CORE_FILES in ace.py; tests/units/tools/module_lists.py checks them).
# Policy: a driver nobody on the project has run on hardware ships with
# default False (opt-in at the user's own risk); once it has field time
# without reports it is switched on by default in a later version.
#
# This module imports no ace.py symbol (ace.py imports it).

from .ace_protocol_v1 import AceProtocolV1
from .ace_protocol_v2 import AceProtocolV2
from .ace_protocol_cfs import AceProtocolCfs


class Driver(object):
    """One registry entry. `tested` is the hardware note the default rests
    on; a driver without it must ship default False."""

    __slots__ = ('name', 'codec', 'option', 'default', 'tested', 'label')

    def __init__(self, name, codec, option, default, tested='', label=''):
        self.name = name
        self.codec = codec
        self.option = option
        self.default = bool(default)
        self.tested = tested
        self.label = label or name

    def __repr__(self):
        return 'Driver(%s, %s, default=%s)' % (
            self.name, self.codec.__name__, self.default)


# Order matters twice: discovery takes the FIRST codec that claims a path,
# and the units of a codec sort by this order unless v2_order says
# otherwise.
DRIVERS = (
    Driver('ace_v1', AceProtocolV1, 'enable_ace_v1', True,
           tested='ACE Pro, in the field since 0.9x', label='AceV1'),
    # The code default stays False (the historical value); the shipped
    # ace.cfg turns it on.
    Driver('ace_v2', AceProtocolV2, 'enable_ace_v2', False,
           tested='ACE 2 / ACE 2 Pro, in the field since 0.97b',
           label='AceV2'),
    # Creality CFS Nano on a USB-RS485 dongle. Protocol measured on one
    # box (wire test 2026-10-26/27); the driver itself has no field time.
    Driver('cfs', AceProtocolCfs, 'enable_cfs', False, label='CFS Nano'),
)

KNOWN_PROTOCOLS = tuple(d.codec for d in DRIVERS)


def driver_named(name):
    """The registry entry of a driver name, or None."""
    for d in DRIVERS:
        if d.name == name:
            return d
    return None


def driver_for(codec_cls):
    """The registry entry of a codec class, or None."""
    for d in DRIVERS:
        if d.codec is codec_cls:
            return d
    return None


def read_enabled_drivers(config):
    """Read every driver's switch from the [ace] section, unconditionally.
    Returns {name: bool} in registry order."""
    enabled = {}
    for d in DRIVERS:
        enabled[d.name] = config.getboolean(d.option, d.default)
    return enabled


def enabled_codecs(enabled):
    """Codec classes of the enabled drivers, in registry order."""
    return tuple(d.codec for d in DRIVERS if enabled.get(d.name, False))


def describe(enabled):
    """One log line: `ace_v1 on, ace_v2 off (untested, opt-in)`."""
    parts = []
    for d in DRIVERS:
        state = 'on' if enabled.get(d.name, False) else 'off'
        if not d.tested:
            state += ' (untested, opt-in)'
        parts.append('%s %s' % (d.name, state))
    return ', '.join(parts)
