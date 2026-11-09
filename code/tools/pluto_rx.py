#!/usr/bin/env python3
"""pluto_rx.py (laptop): ADALM-PLUTO SDR -> LoRa decode (gr-lora_sdr) -> receiver lines on stdout.

We have only one RFM95W (the pod's), so the laptop hears the pod with a Pluto instead of
RFM95W + XIAO #3. This prints the same lines XIAO #3 did (docs/CONTRACTS.md, "Laptop LoRa
receiver"), so lora_rx.py does the filtering and decoding as before:

    conda activate sdr
    python tools/pluto_rx.py --selftest                       # no Pluto: decode one fake heartbeat
    python tools/pluto_rx.py | .venv/bin/python -m afterglow.lora_rx --stdin --print    # real Pluto
    python tools/pluto_rx.py | .venv/bin/python -m afterglow.lora_rx --stdin            # -> ag/alert

Runs in the conda env "sdr" (GNU Radio + gr-lora_sdr + gr-iio), not in .venv: see docs/HARDWARE.md.
Radio settings come from the config's [lora] table, so they always match the pod.
Receive only: it never transmits. rssi and snr are null (an SDR has no calibrated RSSI).
Lines go to stdout; logs go to stderr.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afterglow import alert  # noqa: E402
from afterglow import config as cfgmod  # noqa: E402

log = logging.getLogger("pluto_rx")

PST_SYMBOL = 0x02  # pmt serialisation tag for a symbol (gr-lora_sdr's crc_verif sends the payload as one)


def symbol_bytes(serialized: bytes) -> bytes | None:
    """Payload bytes from pmt.serialize_str(msg). Works for any byte value, unlike symbol_to_string."""
    if len(serialized) < 3 or serialized[0] != PST_SYMBOL:
        return None
    n = int.from_bytes(serialized[1:3], "big")
    body = serialized[3:3 + n]
    return body if len(body) == n else None


def rx_line(packet: bytes) -> str:
    return json.dumps({"type": "rx", "rssi": None, "snr": None, "hex": packet.hex()}, separators=(",", ":"))


def cfg_line(lcfg: dict) -> str:
    return json.dumps({"type": "cfg", "freq": float(lcfg["freq_mhz"]), "sf": int(lcfg["sf"]), "bw": int(lcfg["bw_hz"]),
                       "cr": int(lcfg["cr"]), "pre": int(lcfg["preamble"])}, separators=(",", ":"))


def lora_params(lcfg: dict, samp_rate: int) -> dict:
    """[lora] -> gr-lora_sdr arguments. Its cr is 1-4 for 4/5..4/8; ours is 5-8."""
    if not 5 <= int(lcfg["cr"]) <= 8:
        raise SystemExit(f"[lora] cr must be 5-8 (4/5..4/8), got {lcfg['cr']}")
    if samp_rate % int(lcfg["bw_hz"]):
        raise SystemExit(f"--samp-rate {samp_rate} must be a multiple of bw_hz {lcfg['bw_hz']}")
    return dict(bw=int(lcfg["bw_hz"]), sf=int(lcfg["sf"]), cr=int(lcfg["cr"]) - 4, samp_rate=samp_rate,
                sync_word=[0x12],  # the SX127x default; the pod's adafruit_rfm9x never changes it
                center_freq=int(float(lcfg["freq_mhz"]) * 1e6))


def build(lcfg: dict, samp_rate: int, emit, source=None):
    """The flowgraph: source (the Pluto unless given) -> lora rx -> emit(packet bytes)."""
    import pmt
    from gnuradio import blocks, gr
    from gnuradio.lora_sdr import lora_sdr_lora_rx

    p = lora_params(lcfg, samp_rate)

    class Out(gr.basic_block):
        def __init__(self):
            gr.basic_block.__init__(self, "pluto_rx_out", None, None)
            self.message_port_register_in(pmt.intern("in"))
            self.set_msg_handler(pmt.intern("in"), self.handle)

        def handle(self, msg):
            body = symbol_bytes(pmt.serialize_str(msg))
            if body is not None:
                emit(body)

    tb = gr.top_block()
    rx = lora_sdr_lora_rx(center_freq=p["center_freq"], bw=p["bw"], cr=p["cr"], has_crc=True, impl_head=False,
                          pay_len=255, samp_rate=samp_rate, sf=p["sf"], sync_word=p["sync_word"],
                          soft_decoding=False, ldro_mode=2, print_rx=[False, False])
    out = Out()
    tb.connect(source, rx, blocks.null_sink(gr.sizeof_char))
    tb.msg_connect(rx, "out", out, "in")
    tb._keep = (rx, out, source)  # the Python wrappers must outlive start()
    return tb


def pluto_source(uri: str, lcfg: dict, samp_rate: int, gain):
    from gnuradio import iio
    src = iio.fmcomms2_source_fc32(uri or iio.get_pluto_uri(), [True, True], 32768)
    src.set_len_tag_key("")
    src.set_frequency(int(float(lcfg["freq_mhz"]) * 1e6))
    src.set_samplerate(samp_rate)
    if gain is None:
        src.set_gain_mode(0, "slow_attack")
    else:
        src.set_gain_mode(0, "manual")
        src.set_gain(0, float(gain))
    src.set_quadrature(True)
    src.set_rfdc(True)
    src.set_bbdc(True)
    src.set_filter_params("Auto", "", 0, 0)
    return src


def selftest(lcfg: dict, samp_rate: int) -> int:
    """Modulate one heartbeat exactly as the pod would send it (RadioHead header + 16 bytes),
    add noise and a 10 kHz frequency error, and decode it. No Pluto needed."""
    import pmt
    from gnuradio import blocks, channels
    from gnuradio import lora_sdr as l

    p = lora_params(lcfg, samp_rate)
    packet = bytes([lcfg["laptop_node"], lcfg["pod_node"], 7, 0]) + alert.encode(
        {"x": 1.5, "y": -2.0}, version=lcfg.get("alert_version", 1), kind=alert.TYPE_HEARTBEAT)
    got = []

    # gr-lora_sdr's transmit chain; whitening takes the payload as a hex string
    wh = l.whitening(True, False, ",", "packet_len")
    chain = [wh, l.header(False, True, p["cr"]), l.add_crc(True), l.hamming_enc(p["cr"], p["sf"]),
             l.interleaver(p["cr"], p["sf"], 2, p["bw"]), l.gray_demap(p["sf"]),
             l.modulate(p["sf"], samp_rate, p["bw"], p["sync_word"], 20 * (2 ** p["sf"]), int(lcfg["preamble"])),
             channels.channel_model(noise_voltage=0.05, frequency_offset=10e3 / samp_rate)]
    tb = build(lcfg, samp_rate, got.append, source=chain[-1])
    tb.connect(*chain)
    strobe = blocks.message_strobe(pmt.intern(packet.hex()), 400)
    tb.msg_connect(strobe, "strobe", wh, "msg")
    tb.start()
    time.sleep(3)
    tb.stop()
    tb.wait()
    for b in got[:1]:
        print(rx_line(b), flush=True)
    ok = any(b == packet for b in got)
    log.info("selftest: sent %s, decoded %d packets: %s", packet.hex(), len(got), "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--uri", default="", help="libiio URI, e.g. usb:0.1.5 or ip:192.168.2.1 (default: find the Pluto)")
    ap.add_argument("--gain", type=float, help="manual RX gain in dB, 0-73 (default: AGC). Lower it if the pod is very close")
    ap.add_argument("--samp-rate", type=int, default=1_000_000, help="Pluto sample rate, a multiple of bw_hz (default 1 MS/s)")
    ap.add_argument("--selftest", action="store_true", help="no Pluto: modulate a fake heartbeat and decode it")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    lcfg = cfgmod.load(a.config)["lora"]

    print(cfg_line(lcfg), flush=True)  # lora_rx.py checks it against [lora], as it did XIAO #3's
    if a.selftest:
        sys.exit(selftest(lcfg, a.samp_rate))

    n = [0]

    def emit(b: bytes):
        n[0] += 1
        print(rx_line(b), flush=True)

    tb = build(lcfg, a.samp_rate, emit, source=pluto_source(a.uri, lcfg, a.samp_rate, a.gain))
    tb.start()
    log.info("listening on %.3f MHz, SF%d, %d kHz (gain %s)", lcfg["freq_mhz"], lcfg["sf"], lcfg["bw_hz"] // 1000,
             "AGC" if a.gain is None else f"{a.gain} dB")
    try:
        while True:
            time.sleep(30)
            log.info("packets so far: %d", n[0])
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        tb.stop()
        tb.wait()


if __name__ == "__main__":
    main()
