# SPDX-License-Identifer: GPL-3.0-or-later

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject, GLib
import mimic3_tts
import langcodes
import dbus
import os
import threading
import dbus.service
import dbus.mainloop.glib

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
        self.pitch = Gst.ElementFactory.make("pitch", "pitch")
        self.convert = Gst.ElementFactory.make("audioconvert", "convert")
        self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")
        self.sink.set_property("sync", False)

        # build the pipeline.
        self.pipeline.add(self.sink)
        self.pipeline.add(self.pitch)
        self.pipeline.add(self.convert)
        self.pipeline.add(self.parse)

        self.parse.link(self.convert)
        self.convert.link(self.pitch)
        self.pitch.link(self.sink)

        self.parse.set_property("sample-rate", SAMPLE_RATE)
        self.parse.set_property("num-channels", 1)
        self.pitch.set_property("pitch", 1)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_eos_or_end)
        bus.connect("message::eos", self.on_eos_or_end)
        bus.connect("message::state-changed", self.on_state_changed)

    def on_state_changed(self, bus, msg):
        if msg.src != self.pipeline:
            return

        old, new, pending = msg.parse_state_changed()
        if new == Gst.State.PLAYING:
            self.emit("start")

    def on_eos_or_end(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("ERROR:", msg.src.get_name(), ":", err.message)
            if dbg:
                print("Debug info:", dbg)

        self._reset_pipeline()

    def _reset_pipeline(self):
        rr = os.fdopen(self.source.get_property("fd"), "rb")
        rr.flush()
        rr.close()
        self.source.set_state(Gst.State.NULL)
        self.source.unlink(self.parse)
        self.pipeline.remove(self.source)
        self.source = None
        self.pitch.set_property("pitch", 1)
        self.pipeline.set_state(Gst.State.READY)
        self.emit("end")

    @GObject.Signal
    def start(self):
        pass

    @GObject.Signal
    def end(self):
        pass

    def _synth(self, fd, text, voice_id, rate, volume):
        ww = os.fdopen(fd, "wb", buffering=0)
        with self._tts_lock:
            s = mimic3_tts.Mimic3Settings()
            if voice_id:
                self.mimic3.voice = voice_id
            self.mimic3.rate = rate if rate is not None else s.rate
            self.mimic3.volume = volume * 100 if volume is not None else s.volume
            self.mimic3.begin_utterance()
            self.mimic3.speak_text(text)
            for result in self.mimic3.end_utterance():
                ww.write(result.audio_bytes)
        ww.close()

    def speak(self, text, voice_id=None, pitch=None, rate=None, volume=None):
        self.source = Gst.ElementFactory.make("fdsrc", "source")
        self.pipeline.add(self.source)
        self.source.link(self.parse)
        self.pitch.set_property("pitch", pitch if pitch is not None else 1)

        r, w = os.pipe()
        self.source.set_property("fd", r)

        x = threading.Thread(target=self._synth, args=(w, text, voice_id, rate, volume))
        x.start()

        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self):
        self.pipeline.set_state(Gst.State.PAUSED)

    def resume(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self._reset_pipeline()


class MimicProvider(dbus.service.Object):
    def __init__(self, loop, *args):
        self._last_speak_args = [0, "", "", 0, 0, 0]
        self._tasks = {}
        self._loop = loop
        if not os.environ.get("KEEP_ALIVE"):
            GLib.timeout_add_seconds(AUTO_EXIT_SECONDS, self._timeout)
        s = mimic3_tts.Mimic3Settings()
        self.mimic3 = mimic3_tts.Mimic3TextToSpeechSystem(s)
        self.mimic3.preload_voice(self.mimic3.voice)
        self._worker_pool = []
        for i in range(1):
            worker = Mimic3SynthWorker(self.mimic3)
            self._worker_pool.append(worker)
        dbus.service.Object.__init__(self, *args)

    def _timeout(self):
        if self._tasks:
            return True
        self._loop.quit()
        return False

    @dbus.service.method(
        "org.freedesktop.Speech.Provider",
        in_signature="tssddd",
        out_signature="",
    )
    def Speak(self, task_id, utterance, voice_id, pitch, rate, volume):
        worker = self._worker_pool.pop(0)
        self._tasks[task_id] = worker
        worker.connect("start", self.on_start, task_id)
        worker.connect("end", self.on_end, task_id)
        worker.speak(utterance, voice_id, pitch, rate, volume)

    def on_end(self, worker, task_id):
        worker.disconnect_by_func(self.on_end)
        worker.disconnect_by_func(self.on_start)
        self._worker_pool.append(worker)
        self.SpeechEnd(task_id)

    def on_start(self, worker, task_id):
        self.SpeechStart(task_id)

    @dbus.service.method(
        "org.freedesktop.Speech.Provider",
        in_signature="t",
        out_signature="",
    )
    def Pause(self, task_id):
        worker = self._tasks.get(task_id)
        if worker:
            worker.pause()

    @dbus.service.method(
        "org.freedesktop.Speech.Provider",
        in_signature="t",
        out_signature="",
    )
    def Resume(self, task_id):
        worker = self._tasks.get(task_id)
        if worker:
            worker.resume()

    @dbus.service.method(
        "org.freedesktop.Speech.Provider",
        in_signature="t",
        out_signature="",
    )
    def Cancel(self, task_id):
        worker = self._tasks.get(task_id)
        if worker:
            worker.stop()

    @dbus.service.method(
        "org.freedesktop.Speech.Provider",
        in_signature="",
        out_signature="a(ssas)",
    )
    def GetVoices(self):
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
                            [lang_tag],
                        )
                    )
                else:
                    for speaker in v.speakers:
                        voices.append(
                            (
                                f"{v.name}/{speaker} - {lang_desc}",
                                f"{v.key}#{speaker}",
                                [v.language],
                            )
                        )
        return voices

    @dbus.service.signal("org.freedesktop.Speech.Provider", signature="t")
    def SpeechStart(self, task_id):
        pass

    @dbus.service.signal("org.freedesktop.Speech.Provider", signature="ttt")
    def SpeechRangeStart(self, task_id, start, end):
        pass

    @dbus.service.signal("org.freedesktop.Speech.Provider", signature="t")
    def SpeechEnd(self, task_id):
        pass

    @dbus.service.signal("org.freedesktop.Speech.Provider")
    def VoicesChanged(self):
        pass


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    mainloop = GLib.MainLoop()

    session_bus = dbus.SessionBus()
    name = dbus.service.BusName(f"ai.mimic3.Speech.Provider", session_bus)
    obj = MimicProvider(mainloop, session_bus, f"/ai/mimic3/Speech/Provider")

    mainloop.run()
    return 0
