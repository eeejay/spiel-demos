# SPDX-License-Identifer: GPL-3.0-or-later

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject, GLib
import mimic3_tts
import langcodes
from dasbus.connection import SessionMessageBus
from dasbus.unix import GLibServerUnix
from dasbus.server.interface import dbus_interface
from dasbus.typing import Variant, UnixFD, Str, Double, Bool, List, Tuple, UInt64
import os
import threading

SAMPLE_RATE = 22050
AUTO_EXIT_SECONDS = 120  # Two minute timeout for service

Gst.init(None)


class Mimic3SynthWorker(GObject.Object):
    _tts_lock = threading.Lock()

    def __init__(self, mimic3):
        super().__init__()
        self.mimic3 = mimic3
        # create empty pipeline
        self.pipeline = Gst.Pipeline.new("test-pipeline")

        # create the elements
        self.source = None
        self.parse = Gst.ElementFactory.make("rawaudioparse", "parse")
        self.convert = Gst.ElementFactory.make("audioconvert", "convert")
        self.pitch = Gst.ElementFactory.make("pitch", "pitch")
        self.convert2 = Gst.ElementFactory.make("audioconvert", "convert2")
        self.caps_filter = Gst.ElementFactory.make("capsfilter", "audioconvert_filter")
        self.sink = Gst.ElementFactory.make("fdsink", "sink")
        self.sink.set_property("sync", False)

        elements = [
            self.parse,
            self.convert,
            self.pitch,
            self.convert2,
            self.caps_filter,
            self.sink,
        ]

        for el in elements:
            self.pipeline.add(el)

        for index in range(1, len(elements)):
            elements[index - 1].link(elements[index])

        self.parse.set_property("num-channels", 1)
        self.parse.set_property("sample-rate", SAMPLE_RATE)
        self.caps_filter.set_property(
            "caps",
            Gst.caps_from_string(
                f"audio/x-raw,format=S16LE,channels=1,rate={SAMPLE_RATE}"
            ),
        )
        self.pitch.set_property("pitch", 1)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_eos_or_end)
        bus.connect("message::eos", self.on_eos_or_end)

    def on_eos_or_end(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("ERROR:", msg.src.get_name(), ":", err.message)
            if dbg:
                print("Debug info:", dbg)

        self._reset_pipeline()

    def _reset_pipeline(self):
        if not self.source:
            return
        rr = os.fdopen(self.source.get_property("fd"), "rb")
        rr.flush()
        rr.close()
        os.close(self.sink.get_property("fd"))

        self.source.set_state(Gst.State.NULL)
        self.source.unlink(self.parse)
        self.pipeline.remove(self.source)
        self.source = None
        self.pitch.set_property("pitch", 1)
        self.pipeline.set_state(Gst.State.READY)
        self.emit("done")

    @GObject.Signal
    def done(self):
        pass

    def _synth(self, fd, text, voice_id, rate):
        ww = os.fdopen(fd, "wb", buffering=0)
        with Mimic3SynthWorker._tts_lock:
            s = mimic3_tts.Mimic3Settings()
            if voice_id:
                self.mimic3.voice = voice_id
            self.mimic3.rate = rate if rate is not None else s.rate
            self.mimic3.begin_utterance()
            self.mimic3.speak_text(text)
            for result in self.mimic3.end_utterance():
                ww.write(result.audio_bytes)
        ww.close()

    def synthesize(self, fd, text, voice_id=None, pitch=None, rate=None,):
        self.sink.set_property("fd", fd)
        self.source = Gst.ElementFactory.make("fdsrc", "source")
        self.pipeline.add(self.source)
        self.source.link(self.parse)
        self.pitch.set_property("pitch", pitch if pitch is not None else 1)

        r, w = os.pipe()
        self.source.set_property("fd", r)

        x = threading.Thread(target=self._synth, args=(w, text, voice_id, rate))
        x.start()

        self.pipeline.set_state(Gst.State.PLAYING)


@dbus_interface("org.freedesktop.Speech.Provider")
class MimicProvider(object):
    def __init__(self, loop):
        self._last_speak_args = [0, "", "", 0, 0, 0]
        self._loop = loop
        if not os.environ.get("KEEP_ALIVE"):
            GLib.timeout_add_seconds(AUTO_EXIT_SECONDS, self._timeout)
        s = mimic3_tts.Mimic3Settings()
        self.mimic3 = mimic3_tts.Mimic3TextToSpeechSystem(s)
        self.mimic3.preload_voice(self.mimic3.voice)
        self._worker_pool = []
        for i in range(5):
            worker = Mimic3SynthWorker(self.mimic3)
            worker.connect("done", self._on_done)
            self._worker_pool.append(worker)

    def _timeout(self):
        if len(self._worker_pool) < 5:
            return True
        self._loop.quit()
        return False

    def _on_done(self, worker):
        self._worker_pool.append(worker)

    def Synthesize(
        self,
        fd: UnixFD,
        utterance: Str,
        voice_id: Str,
        pitch: Double,
        rate: Double,
        is_ssml: Bool,
    ):
        worker = self._worker_pool.pop(0)
        worker.synthesize(fd, utterance, voice_id, pitch, rate)

    @property
    def Voices(self) -> List[Tuple[Str, Str, Str, UInt64, List[Str]]]:
        voices = []
        for v in self.mimic3.get_voices():
            # if v.location.startswith("/"):
            if True:
                lang_tag = langcodes.standardize_tag(v.language)
                lang_desc = langcodes.Language.get(lang_tag).display_name()
                if not v.speakers:
                    voices.append(
                        (
                            f"{v.name} - {lang_desc}",
                            v.key,
                            f"audio/x-raw,format=S16LE,channels=1,rate={SAMPLE_RATE}",
                            0,
                            [lang_tag],
                        )
                    )
                else:
                    for speaker in v.speakers:
                        voices.append(
                            (
                                f"{v.name}/{speaker} - {lang_desc}",
                                f"{v.key}#{speaker}",
                                f"audio/x-raw,format=S16LE,channels=1,rate={SAMPLE_RATE}",
                                0,
                                [v.language],
                            )
                        )
        return voices

def main():
    mainloop = GLib.MainLoop()

    bus = SessionMessageBus()
    bus.publish_object(
        "/ai/mimic3/Speech/Provider",
        MimicProvider(mainloop),
        server=GLibServerUnix,
     )
    bus.register_service("ai.mimic3.Speech.Provider")

    mainloop.run()
    return 0
