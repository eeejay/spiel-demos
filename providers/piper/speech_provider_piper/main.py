# SPDX-License-Identifer: GPL-3.0-or-later

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject, GLib
from piper import PiperVoice
from pathlib import Path
import dbus
import os
import json
import threading
import dbus.service
import dbus.mainloop.glib

AUTO_EXIT_SECONDS = 120  # Two minute timeout for service

Gst.init(None)


class PiperSynthWorker(GObject.Object):
    _tts_lock = threading.Lock()

    def __init__(self, voices_dir):
        super().__init__()
        self.voices_dir = voices_dir
        self.cached_voice = ("", None)

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
        _voice_id, voice = self.cached_voice
        if not _voice_id or _voice_id != voice_id:
            voice = PiperVoice.load((self.voices_dir / voice_id).with_suffix(".onnx"))
            self.cached_voice = (voice_id, voice)

        self.parse.set_property("sample-rate", voice.config.sample_rate)

        length_scale = voice.config.length_scale / rate
        ww = os.fdopen(fd, "wb", buffering=0)
        with self._tts_lock:
            audio_stream = voice.synthesize_stream_raw(text, length_scale=length_scale)
            for audio_bytes in audio_stream:
                ww.write(audio_bytes)
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


class PiperProvider(dbus.service.Object):
    def __init__(self, loop, default_voices_dir, *args):
        self._last_speak_args = [0, "", "", 0, 0, 0]
        self._tasks = {}
        self._loop = loop
        if not os.environ.get("KEEP_ALIVE"):
            GLib.timeout_add_seconds(AUTO_EXIT_SECONDS, self._timeout)
        self.voices_dir = Path(os.environ.get("PIPER_VOICES_DIR", default_voices_dir))
        if not self.voices_dir.is_absolute():
            self.voices_dir = Path.cwd() / self.voices_dir
        self._worker_pool = []
        for i in range(1):
            worker = PiperSynthWorker(self.voices_dir)
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
        del self._tasks[task_id]

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
        for voice_config in self.voices_dir.glob("*.onnx.json"):
            config = json.loads(voice_config.read_text())
            voices.append(
                (
                    f"{config['dataset']} ({config['language']['name_native']})",
                    voice_config.stem[:-5],
                    [config["language"]["code"].replace("_", "-")],
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


def main(default_voices_dir):
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    mainloop = GLib.MainLoop()

    session_bus = dbus.SessionBus()
    name = dbus.service.BusName(f"ai.piper.Speech.Provider", session_bus)
    obj = PiperProvider(
        mainloop, default_voices_dir, session_bus, f"/ai/piper/Speech/Provider"
    )

    mainloop.run()
    return 0
