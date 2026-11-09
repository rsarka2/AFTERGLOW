"""The pod's LoRa radio: an Adafruit RFM95W (SX1276) on the Pi's SPI1. Pi only.

    rfm = open_pod_radio(cfg["lora"])
    ok = rfm.send(alert.encode(target))   # header: to laptop_node, from pod_node

Settings come from the config's [lora] table; firmware/lora_rx/lora_rx.ino must match.
Plain LoRa, no encryption: an alert carries a pin, never a device address or ID.
lora_out.py and tools/check_hw.py both open the radio through here.
"""


_claimed = []  # SPI1 and the CS and RST pins from the last open, until close_pod_radio()


def open_pod_radio(lcfg: dict):
    """Return a configured adafruit_rfm9x.RFM9x. Raises RuntimeError if the radio doesn't answer.
    Opening again (lora_out's retry) first frees what the last open claimed."""
    import adafruit_rfm9x  # Blinka + rfm9x; imported here so the package still imports on a laptop
    import board
    import busio
    import digitalio

    close_pod_radio()
    try:
        spi = busio.SPI(board.SCK_1, MOSI=board.MOSI_1, MISO=board.MISO_1)
        _claimed.append(spi)
        cs = digitalio.DigitalInOut(getattr(board, f"D{lcfg['cs_gpio']}"))
        _claimed.append(cs)
        reset = digitalio.DigitalInOut(getattr(board, f"D{lcfg['reset_gpio']}"))
        _claimed.append(reset)
        rfm = adafruit_rfm9x.RFM9x(spi, cs, reset, lcfg["freq_mhz"], preamble_length=lcfg["preamble"])
        rfm.spreading_factor = lcfg["sf"]
        rfm.signal_bandwidth = lcfg["bw_hz"]
        rfm.coding_rate = lcfg["cr"]
        rfm.tx_power = lcfg["tx_power_dbm"]
        rfm.enable_crc = True
        rfm.node = lcfg["pod_node"]
        rfm.destination = lcfg["laptop_node"]
    except Exception:
        close_pod_radio()  # a radio that didn't answer mustn't keep its pins, or the retry finds them busy
        raise
    return rfm


def close_pod_radio():
    """Free SPI1 and the CS and RST pins: Blinka keeps them claimed until deinit()."""
    while _claimed:
        part = _claimed.pop()
        try:
            part.deinit()
        except Exception:  # already freed: nothing more to do
            pass
