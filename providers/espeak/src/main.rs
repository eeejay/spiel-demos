use espeaker;
use futures::executor::block_on;
use lazy_static::lazy_static;
use rodio::{OutputStream, Sink};
use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap;
use std::future::pending;
use std::hash::Hasher;
use std::sync::Mutex;
use std::thread;
use zbus::{dbus_interface, ConnectionBuilder, MessageHeader, Result, SignalContext};

const NORMAL_RATE: f32 = 175.0;

struct TaskEntry {
    _stream: OutputStream,
    sink: Sink,
    espeaker: espeaker::Speaker,
}

unsafe impl Sync for TaskEntry {}
unsafe impl Send for TaskEntry {}

lazy_static! {
    static ref TASKS: Mutex<HashMap<u64, TaskEntry>> = Mutex::new(HashMap::new());
}

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

    fn task_hash(sender: &str, task_id: u64) -> u64 {
        let mut s = DefaultHasher::new();
        s.write(sender.as_bytes());
        s.write(&task_id.to_ne_bytes());
        s.finish()
    }
}

#[dbus_interface(name = "org.freedesktop.Speech.Provider")]
impl Speaker {
    async fn get_voices(&self) -> Vec<(String, String, Vec<String>)> {
        println!("get_voices");
        espeaker::list_voices()
            .into_iter()
            .map(|v| {
                (
                    v.name,
                    v.identifier,
                    v.languages.into_iter().map(|l| l.name).collect(),
                )
            })
            .collect()
    }

    async fn speak(
        &mut self,
        task_id: u64,
        utterance: &str,
        voice_id: &str,
        pitch: f32,
        rate: f32,
        volume: f32,
        #[zbus(header)] header: MessageHeader<'_>,
        #[zbus(signal_context)] ctxt: SignalContext<'_>,
    ) {
        println!(
            "speak '{}' {} {} {}\n  {:?}",
            utterance,
            pitch,
            rate,
            volume,
            thread::current()
        );
        // println!("header '{:?}'", header);
        let sender = String::from(header.sender().unwrap().unwrap().to_owned().as_str());
        let task_key = Speaker::task_hash(sender.as_str(), task_id);
        let s = String::from(utterance);
        let owned_ctxt = ctxt.into_owned();
        let (_stream, stream_handle) = OutputStream::try_default().unwrap();
        let sink = Sink::try_new(&stream_handle).unwrap();
        let mut espeaker = espeaker::Speaker::new();
        if !voice_id.is_empty() {
            let voice = espeaker::list_voices()
                .into_iter()
                .find(|v| v.identifier == voice_id)
                .unwrap();
            espeaker.set_voice(&voice);
        }
        espeaker.params.volume = Some((volume * 100.0).round() as i32);
        espeaker.params.pitch = Some((pitch * 50.0).round() as i32);
        espeaker.params.rate = Some((rate * NORMAL_RATE).round() as i32);
        let task_entry = TaskEntry {
            _stream,
            sink,
            espeaker,
        };
        let source = task_entry.espeaker.speak(&s);
        let source = source.with_callback(move |evt| match evt {
            espeaker::Event::Start => {
                block_on(Speaker::speech_start(&owned_ctxt, task_id)).unwrap();
            }
            espeaker::Event::Word(start, len) => {
                block_on(Speaker::speech_range_start(
                    &owned_ctxt,
                    task_id,
                    start as u64,
                    (start + len) as u64,
                ))
                .unwrap();
            }
            espeaker::Event::Sentence(_) => (),
            espeaker::Event::End => {
                block_on(Speaker::speech_end(&owned_ctxt, task_id)).unwrap();
                thread::spawn(move || {
                    let mut tasks = TASKS.lock().unwrap();
                    tasks.remove(&task_key);
                });
            }
        });
        task_entry.sink.append(source);
        let mut tasks = TASKS.lock().unwrap();
        tasks.insert(task_key, task_entry);
    }

    async fn cancel(
        &self,
        task_id: u64,
        #[zbus(header)] header: MessageHeader<'_>,
        #[zbus(signal_context)] _ctxt: SignalContext<'_>,
    ) {
        let sender = String::from(header.sender().unwrap().unwrap().to_owned().as_str());
        let tasks = TASKS.lock().unwrap();
        let task_key = Speaker::task_hash(sender.as_str(), task_id);

        match tasks.get(&task_key) {
            None => (),
            Some(entry) => entry.sink.stop(),
        }
    }

    async fn pause(
        &self,
        task_id: u64,
        #[zbus(header)] header: MessageHeader<'_>,
        #[zbus(signal_context)] _ctxt: SignalContext<'_>,
    ) {
        let sender = String::from(header.sender().unwrap().unwrap().to_owned().as_str());
        let tasks = TASKS.lock().unwrap();
        let task_key = Speaker::task_hash(sender.as_str(), task_id);

        match tasks.get(&task_key) {
            None => (),
            Some(entry) => entry.sink.pause(),
        }
    }

    async fn resume(
        &self,
        task_id: u64,
        #[zbus(header)] header: MessageHeader<'_>,
        #[zbus(signal_context)] _ctxt: SignalContext<'_>,
    ) {
        let sender = String::from(header.sender().unwrap().unwrap().to_owned().as_str());
        let tasks = TASKS.lock().unwrap();
        let task_key = Speaker::task_hash(sender.as_str(), task_id);

        match tasks.get(&task_key) {
            None => (),
            Some(entry) => entry.sink.play(),
        }
    }

    #[dbus_interface(signal)]
    async fn speech_start(ctxt: &SignalContext<'_>, task_id: u64) -> Result<()>;

    #[dbus_interface(signal)]
    async fn speech_end(ctxt: &SignalContext<'_>, task_id: u64) -> Result<()>;

    #[dbus_interface(signal)]
    async fn speech_range_start(
        ctxt: &SignalContext<'_>,
        task_id: u64,
        start: u64,
        end: u64,
    ) -> Result<()>;
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
