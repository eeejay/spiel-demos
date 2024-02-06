use espeaker;
use std::future::pending;
use std::thread;
use zbus::{dbus_interface, ConnectionBuilder, MessageHeader, Result, SignalContext};
use zvariant::OwnedFd;
use std::os::fd::IntoRawFd;

/// No-op.
macro_rules! assert_initialized_main_thread {
    () => {};
}

/// No-op.
macro_rules! skip_assert_initialized {
    () => {};
}

use auto::*;
mod auto;

const NORMAL_RATE: f32 = 175.0;

pub struct Voice {
    pub name: String,
    pub identifier: String,
    pub languages: Vec<String>,
}

struct Speaker {}

impl Speaker {
    fn new() -> Speaker {
        Speaker {}
    }
}

#[dbus_interface(name = "org.freedesktop.Speech.Provider")]
impl Speaker {
    async fn get_voices(&self) -> Vec<(String, String, String, u64, Vec<String>)> {
        let features = VoiceFeature::EVENTS_WORD | VoiceFeature::EVENTS_SENTENCE;
        espeaker::list_voices()
            .into_iter()
            .map(|v| {
                (
                    v.name,
                    v.identifier,
                    "audio/x-spiel,format=S16LE,channels=1,rate=22050".to_string(),
                    features.bits() as u64,
                    v.languages.into_iter().map(|l| l.name).collect(),
                )
            })
            .collect()
    }

    async fn synthesize(
        &mut self,
        fd: OwnedFd,
        utterance: &str,
        voice_id: &str,
        pitch: f32,
        rate: f32,
        is_ssml: bool,
        #[zbus(header)] _header: MessageHeader<'_>,
        #[zbus(signal_context)] _ctxt: SignalContext<'_>,
    ) {
        let s = String::from(utterance);
    
        let voice = espeaker::list_voices()
        .into_iter()
        .find(|v| v.identifier == voice_id)
        .unwrap();

        thread::spawn(move || {
            let mut espeaker = espeaker::Speaker::new();                
            espeaker.set_voice(&voice);
            espeaker.params.pitch = Some((pitch * 50.0).round() as i32);
            espeaker.params.rate = Some((rate * NORMAL_RATE).round() as i32);

            let raw_fd = fd.into_raw_fd();
            // let mut f = File::from(owned_fd);
            let source = espeaker.speak(&s);
            let stream_writer = StreamWriter::new(raw_fd);
            stream_writer.send_stream_header();
            let mut buffer = [0u8; 2048];
            let mut index = 0;
            for (sample, events) in source.iter_audio_and_events() {
                match events {
                    None => (),
                    Some(events) => {
                        for evt in events {
                            match evt {
                                espeaker::Event::Start => (),
                                espeaker::Event::Word(start, len) => {
                                    stream_writer.send_event(EventType::Word, start as u32, (start + len) as u32, "");
                                },
                                espeaker::Event::Sentence(_) => (),
                                espeaker::Event::End => ()
                            }
                        }
                        if index > 0 {
                            stream_writer.send_audio(&buffer[..index-2]);
                        }
                        index = 0;
                    }
                }
                [buffer[index], buffer[index+1]] = sample.to_le_bytes();
                index += 2;
                if index >= 2048 {
                    stream_writer.send_audio(&buffer);
                    index = 0;
                }
            }
            stream_writer.send_audio(&buffer[..index-2]);
        });
    }
}

// Although we use `async-std` here, you can use any async runtime of choice.
#[async_std::main]
async fn main() -> Result<()> {
    let _conn = ConnectionBuilder::session()?
        .name("org.espeak.Speech.Provider")?
        .serve_at("/org/espeak/Speech/Provider", Speaker::new())?
        .build()
        .await?;

    // Do other things or go to wait forever
    pending::<()>().await;

    Ok(())
}
