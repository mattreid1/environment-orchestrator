//! A remote-only Codex gateway. Idle WebSockets do not own execution leases.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use futures_util::stream::SplitSink;
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use subtle::ConstantTimeEq;
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::time::{Instant, timeout, timeout_at};
use tokio_tungstenite::tungstenite::Message as GuestMessage;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async_with_config};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::manager::{Binding, Lease, Manager, WriterGuard};

const MAX_MESSAGE: usize = 64 * 1024 * 1024;
const MAX_PENDING: usize = 256;
const IO_TIMEOUT: Duration = Duration::from_secs(10);
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(5);
// Exec-server retains detached sessions for 30 seconds. Keep the VM awake
// until that cancellation window ends if an operation has an unknown result.
const UNKNOWN_GRACE: Duration = Duration::from_secs(35);

type GuestSocket = WebSocketStream<MaybeTlsStream<TcpStream>>;
type GuestSink = SplitSink<GuestSocket, GuestMessage>;
type HarnessSink = SplitSink<WebSocket, Message>;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
enum RpcId {
    Integer(i64),
    String(String),
}

impl RpcId {
    fn parse(value: &Value) -> Result<Self> {
        match value {
            Value::String(value) => Ok(Self::String(value.clone())),
            Value::Number(value) => value
                .as_i64()
                .map(Self::Integer)
                .ok_or_else(|| anyhow!("RPC IDs must be strings or signed integers")),
            _ => bail!("RPC IDs must be strings or signed integers"),
        }
    }
}

#[derive(Clone, Debug)]
struct PendingRpc {
    id: RpcId,
    method: String,
    process_id: Option<String>,
    operation_key: String,
}

#[derive(Default, Debug)]
struct RpcTracking {
    pending: HashMap<RpcId, PendingRpc>,
    processes: HashSet<String>,
    guest_requests: HashSet<RpcId>,
    initialized: bool,
    environment_info: Option<Value>,
}

impl RpcTracking {
    fn prepare(&self, data: &Value, connection: &str) -> Result<Option<PendingRpc>> {
        if !data.is_object() {
            bail!("RPC messages must be objects");
        }
        let Some(method) = data.get("method") else {
            return Ok(None);
        };
        let method = method
            .as_str()
            .filter(|method| !method.is_empty())
            .ok_or_else(|| anyhow!("RPC methods must be nonempty strings"))?;
        let process_id = data
            .get("params")
            .and_then(|params| params.get("processId"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        if method == "process/start" {
            let process = process_id
                .as_deref()
                .filter(|process| !process.is_empty())
                .ok_or_else(|| anyhow!("process/start requires a processId"))?;
            if self.processes.contains(process) {
                bail!("Process ID already belongs to an active operation");
            }
        }
        let Some(identifier) = data.get("id") else {
            if method == "process/start" {
                bail!("process/start requires an RPC ID");
            }
            return Ok(None);
        };
        let id = RpcId::parse(identifier)?;
        if self.pending.contains_key(&id) {
            bail!("RPC ID already belongs to a pending operation");
        }
        if self.pending.len() >= MAX_PENDING {
            bail!("Too many pending operations");
        }
        Ok(Some(PendingRpc {
            id,
            method: method.to_owned(),
            process_id,
            // RPC IDs can be reused after completion. Each durable operation
            // therefore has its own key, rather than connection + RPC ID.
            operation_key: format!("{connection}:{}", Uuid::new_v4()),
        }))
    }

    fn insert(&mut self, pending: PendingRpc) {
        if pending.method == "process/start"
            && let Some(process) = &pending.process_id
        {
            self.processes.insert(process.clone());
        }
        self.pending.insert(pending.id.clone(), pending);
    }

    fn harness_response(&mut self, data: &Value) {
        if data.get("method").is_none()
            && (data.get("result").is_some() || data.get("error").is_some())
            && let Some(id) = data.get("id").and_then(|id| RpcId::parse(id).ok())
        {
            self.guest_requests.remove(&id);
        }
    }

    fn guest_message(&mut self, data: &Value) -> Option<(String, &'static str)> {
        if let Some(method) = data.get("method").and_then(Value::as_str) {
            if let Some(id) = data.get("id").and_then(|id| RpcId::parse(id).ok()) {
                self.guest_requests.insert(id);
            }
            if matches!(method, "process/exited" | "process/closed")
                && let Some(process) = data
                    .get("params")
                    .and_then(|params| params.get("processId"))
                    .and_then(Value::as_str)
            {
                self.processes.remove(process);
            }
            // Exec-server may request callbacks from the harness. A request
            // with the same ID as a client request is not its response.
            return None;
        }
        if data.get("result").is_none() && data.get("error").is_none() {
            return None;
        }
        let id = data.get("id").and_then(|id| RpcId::parse(id).ok())?;
        let operation = self.pending.remove(&id)?;
        if data.get("error").is_some() {
            if operation.method == "process/start"
                && let Some(process) = &operation.process_id
            {
                self.processes.remove(process);
            }
            return Some((operation.operation_key, "error"));
        }
        if operation.method == "initialize" {
            self.initialized = true;
            self.environment_info = data
                .get("result")
                .and_then(|result| result.get("environmentInfo"))
                .cloned();
        } else if operation.method == "environment/info" {
            self.environment_info = data.get("result").cloned();
        } else if operation.method == "process/read"
            && data
                .get("result")
                .and_then(|result| result.get("exited"))
                .and_then(Value::as_bool)
                == Some(true)
            && let Some(process) = &operation.process_id
        {
            self.processes.remove(process);
        }
        Some((operation.operation_key, "completed"))
    }

    fn local_health_reply(&self, data: &Value) -> Option<Value> {
        if !self.initialized {
            return None;
        }
        let id = data.get("id")?;
        let identifier = RpcId::parse(id).ok()?;
        if self.pending.contains_key(&identifier) {
            return None;
        }
        let result = match data.get("method").and_then(Value::as_str)? {
            "environment/status" => json!({"status": "ready"}),
            "environment/info" => self.environment_info.clone()?,
            _ => return None,
        };
        Some(json!({"id": id, "result": result}))
    }

    fn idle(&self) -> bool {
        self.pending.is_empty() && self.processes.is_empty() && self.guest_requests.is_empty()
    }
}

pub async fn handler(
    State(manager): State<Arc<Manager>>,
    Path(id): Path<String>,
    headers: HeaderMap,
    upgrade: WebSocketUpgrade,
) -> Response {
    let binding = match manager.binding(&id) {
        Ok(binding) => binding,
        Err(_) => return (StatusCode::NOT_FOUND, "Workspace does not exist").into_response(),
    };
    let supplied = headers
        .get("Authorization")
        .and_then(|header| header.to_str().ok())
        .and_then(|header| header.strip_prefix("Bearer "))
        .unwrap_or("");
    if !bool::from(supplied.as_bytes().ct_eq(binding.token.as_bytes())) {
        return (StatusCode::UNAUTHORIZED, "Workspace capability is required").into_response();
    }
    let writer = match manager.claim_writer(&id) {
        Ok(writer) => writer,
        Err(_) => {
            return (
                StatusCode::CONFLICT,
                "This workspace already has an attached harness or is stopping",
            )
                .into_response();
        }
    };
    upgrade
        .max_message_size(MAX_MESSAGE)
        .max_frame_size(MAX_MESSAGE)
        .on_upgrade(move |socket| async move {
            session(manager, binding, socket, writer).await;
        })
        .into_response()
}

enum Event {
    Harness(Message),
    Guest(GuestMessage),
    GuestClosed,
}

impl Event {
    fn byte_size(&self) -> usize {
        match self {
            Self::Harness(Message::Text(text)) => text.len(),
            Self::Harness(Message::Binary(bytes) | Message::Ping(bytes) | Message::Pong(bytes)) => {
                bytes.len()
            }
            Self::Guest(GuestMessage::Text(text)) => text.len(),
            Self::Guest(
                GuestMessage::Binary(bytes) | GuestMessage::Ping(bytes) | GuestMessage::Pong(bytes),
            ) => bytes.len(),
            _ => 0,
        }
    }
}

fn enqueue(sender: &mpsc::Sender<Event>, queued_bytes: &AtomicUsize, event: Event) -> bool {
    let size = event.byte_size();
    let old = queued_bytes.fetch_add(size, Ordering::Relaxed);
    if old.saturating_add(size) > MAX_MESSAGE || sender.try_send(event).is_err() {
        queued_bytes.fetch_sub(size, Ordering::Relaxed);
        return false;
    }
    true
}

async fn send_harness(sink: &mut HarnessSink, message: Message) -> Result<()> {
    timeout(IO_TIMEOUT, sink.send(message))
        .await
        .context("Harness write timed out")??;
    Ok(())
}

async fn send_guest(sink: &mut GuestSink, message: GuestMessage) -> Result<()> {
    timeout(IO_TIMEOUT, sink.send(message))
        .await
        .context("Guest write timed out")??;
    Ok(())
}

fn finish_operation(manager: &Manager, outcome: Option<(String, &'static str)>) {
    if let Some((key, state)) = outcome
        && let Err(error) = manager.operation_finish(&key, state)
    {
        tracing::error!(%error, "Could not persist an operation result");
    }
}

async fn session(manager: Arc<Manager>, binding: Binding, socket: WebSocket, writer: WriterGuard) {
    let workspace = binding.id.clone();
    let connection = Uuid::new_v4().to_string();
    let cancel = manager.shutdown_token.child_token();
    let stop_guest_reader = CancellationToken::new();
    let (events_tx, mut events_rx) = mpsc::channel(MAX_PENDING + 16);
    let queued_bytes = Arc::new(AtomicUsize::new(0));
    let (mut harness_sink, mut harness_stream) = socket.split();
    let harness_cancel = cancel.clone();
    let harness_tx = events_tx.clone();
    let harness_bytes = Arc::clone(&queued_bytes);
    // Start this pump before admission. It detects disconnected clients while
    // the manager waits for RAM and prevents their queued requests from running.
    let harness_reader = tokio::spawn(async move {
        loop {
            let message = tokio::select! {
                biased;
                _ = harness_cancel.cancelled() => break,
                message = harness_stream.next() => message,
            };
            match message {
                Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                Some(Ok(message)) => {
                    // Never block this reader on the event queue: a close
                    // behind buffered requests must still cancel admission.
                    // Oversized queues close the connection without executing
                    // unaccepted operations, rather than consuming host RAM.
                    if !enqueue(&harness_tx, &harness_bytes, Event::Harness(message)) {
                        break;
                    }
                }
            }
        }
        harness_cancel.cancel();
    });
    let mut lease: Option<Lease> = None;
    let mut guest_sink: Option<GuestSink> = None;
    let mut guest_reader = None;
    let mut guest_closed = false;
    let mut tracking = RpcTracking::default();

    let result: Result<()> = async {
        lease = Some(manager.acquire(&workspace, &cancel).await?);
        if cancel.is_cancelled() {
            bail!("Harness disconnected before execution");
        }
        let mut config = WebSocketConfig::default();
        config.max_message_size = Some(MAX_MESSAGE);
        config.max_frame_size = Some(MAX_MESSAGE);
        let connect = connect_async_with_config(
            format!("ws://{}:8765/", binding.guest_host),
            Some(config),
            true,
        );
        let (socket, _) = tokio::select! {
            biased;
            _ = cancel.cancelled() => bail!("Harness disconnected before execution"),
            result = timeout(IO_TIMEOUT, connect) => result.context("Guest connection timed out")??,
        };
        let (sink, mut stream) = socket.split();
        guest_sink = Some(sink);
        let guest_tx = events_tx.clone();
        let guest_bytes = Arc::clone(&queued_bytes);
        let guest_cancel = cancel.clone();
        let reader_stop = stop_guest_reader.clone();
        guest_reader = Some(tokio::spawn(async move {
            loop {
                let message = tokio::select! {
                    biased;
                    _ = reader_stop.cancelled() => return,
                    message = stream.next() => message,
                };
                let event = match message {
                    Some(Ok(GuestMessage::Close(_))) | Some(Err(_)) | None => Event::GuestClosed,
                    Some(Ok(message)) => Event::Guest(message),
                };
                let terminal = matches!(event, Event::GuestClosed);
                if !enqueue(&guest_tx, &guest_bytes, event) {
                    guest_cancel.cancel();
                    return;
                }
                if terminal {
                    return;
                }
            }
        }));
        // Startup owns a lease only through socket connection. Initialization
        // then obtains a request lease, like every other nonlocal RPC. A client
        // that connects without initializing must not keep its VM awake.
        drop(lease.take());
        loop {
            let event = tokio::select! {
                biased;
                _ = cancel.cancelled() => break,
                event = events_rx.recv() => event.ok_or_else(|| anyhow!("Proxy reader stopped"))?,
            };
            queued_bytes.fetch_sub(event.byte_size(), Ordering::Relaxed);
            match event {
                Event::Harness(Message::Text(text)) => {
                    let data: Value = match serde_json::from_str(&text) {
                        Ok(data) => data,
                        Err(_) => {
                            send_harness(&mut harness_sink, Message::Text(json!({
                                "id": null, "error": {"code": -32700, "message": "Invalid RPC JSON"}
                            }).to_string().into())).await?;
                            continue;
                        }
                    };
                    if let Some(reply) = tracking.local_health_reply(&data) {
                        send_harness(&mut harness_sink, Message::Text(reply.to_string().into()))
                            .await?;
                        continue;
                    }
                    let prepared = match tracking.prepare(&data, &connection) {
                        Ok(prepared) => prepared,
                        Err(error) => {
                            send_harness(
                                &mut harness_sink,
                                Message::Text(
                                    json!({
                                        "id": data.get("id").cloned().unwrap_or(Value::Null),
                                        "error": {"code": -32602, "message": error.to_string()}
                                    })
                                    .to_string()
                                    .into(),
                                ),
                            )
                            .await?;
                            continue;
                        }
                    };
                    if lease.is_none() {
                        lease = Some(manager.acquire(&workspace, &cancel).await?);
                    }
                    if cancel.is_cancelled() {
                        break;
                    }
                    if let Some(operation) = prepared {
                        manager.operation_begin(
                            &operation.operation_key,
                            &workspace,
                            &operation.method,
                        )?;
                        tracking.insert(operation);
                    }
                    send_guest(
                        guest_sink.as_mut().unwrap(),
                        GuestMessage::Text(text.as_str().into()),
                    )
                    .await?;
                    tracking.harness_response(&data);
                    if tracking.idle() {
                        drop(lease.take());
                    }
                }
                Event::Harness(Message::Ping(payload)) => {
                    send_harness(&mut harness_sink, Message::Pong(payload)).await?;
                }
                Event::Harness(Message::Pong(_)) => {}
                Event::Harness(Message::Binary(_)) => bail!("Binary RPC frames are unsupported"),
                Event::Harness(Message::Close(_)) => break,
                Event::Guest(GuestMessage::Text(text)) => {
                    let data: Value =
                        serde_json::from_str(&text).context("Invalid guest RPC JSON")?;
                    finish_operation(&manager, tracking.guest_message(&data));
                    if !tracking.idle() && lease.is_none() {
                        lease = Some(manager.acquire(&workspace, &cancel).await?);
                    }
                    send_harness(&mut harness_sink, Message::Text(text.as_str().into())).await?;
                    if tracking.idle() {
                        drop(lease.take());
                    }
                }
                Event::Guest(GuestMessage::Ping(payload)) => {
                    // There is no heartbeat deadline. A paused VM's TCP socket
                    // remains attached, and its ping traffic does not wake it.
                    send_guest(guest_sink.as_mut().unwrap(), GuestMessage::Pong(payload)).await?;
                }
                Event::Guest(GuestMessage::Pong(_) | GuestMessage::Frame(_)) => {}
                Event::Guest(GuestMessage::Binary(_)) => {
                    bail!("Binary guest RPC frames are unsupported")
                }
                Event::Guest(GuestMessage::Close(_)) | Event::GuestClosed => {
                    guest_closed = true;
                    bail!("Guest disconnected; no operation will be replayed");
                }
            }
        }
        Ok(())
    }
    .await;

    if let Err(error) = result {
        tracing::warn!(%workspace, %error, "Execution connection failed; requests will not be replayed");
    }
    // Keep the writer and lease while cleanup runs. The guest reader remains
    // alive so process/exited notifications can confirm termination.
    let deadline = Instant::now() + CLEANUP_TIMEOUT;
    if let Some(sink) = guest_sink.as_mut() {
        if !guest_closed {
            for process in tracking.processes.iter() {
                let command = GuestMessage::Text(
                    json!({
                        "id": format!("orchestrator-cancel-{}", Uuid::new_v4()),
                        "method": "process/terminate", "params": {"processId": process}
                    })
                    .to_string()
                    .into(),
                );
                if !matches!(timeout_at(deadline, sink.send(command)).await, Ok(Ok(()))) {
                    break;
                }
            }
        }
        while !tracking.idle() && !guest_closed {
            let event = match timeout_at(deadline, events_rx.recv()).await {
                Ok(Some(event)) => event,
                _ => break,
            };
            queued_bytes.fetch_sub(event.byte_size(), Ordering::Relaxed);
            match event {
                Event::Guest(GuestMessage::Text(text)) => {
                    if let Ok(data) = serde_json::from_str(&text) {
                        finish_operation(&manager, tracking.guest_message(&data));
                    }
                }
                Event::Guest(GuestMessage::Ping(payload)) => {
                    if !matches!(
                        timeout_at(deadline, sink.send(GuestMessage::Pong(payload))).await,
                        Ok(Ok(()))
                    ) {
                        break;
                    }
                }
                Event::GuestClosed | Event::Guest(GuestMessage::Close(_)) => guest_closed = true,
                _ => {}
            }
        }
        let _ = timeout_at(deadline, sink.close()).await;
    }
    for operation in tracking.pending.values() {
        if let Err(error) = manager.operation_finish(&operation.operation_key, "unknown") {
            tracing::error!(%error, "Could not persist an unknown operation result");
        }
    }
    let uncertain = !tracking.idle();
    stop_guest_reader.cancel();
    if let Some(reader) = guest_reader {
        reader.abort();
        let _ = reader.await;
    }
    // Drop both TCP halves before starting the detached-session grace period.
    drop(guest_sink.take());
    cancel.cancel();
    harness_reader.abort();
    let _ = harness_reader.await;
    // Release buffered frames before an uncertain-operation grace period.
    // Their contents are not accepted operations and must never be replayed.
    drop(events_rx);
    let _ = timeout(
        IO_TIMEOUT,
        harness_sink.send(Message::Close(Some(CloseFrame {
            code: 1011,
            reason: "Execution connection closed. No operation was replayed.".into(),
        }))),
    )
    .await;
    if uncertain && lease.is_some() {
        tracing::warn!(%workspace, "Preserving the execution lease until the detached session expires");
        tokio::time::sleep(UNKNOWN_GRACE).await;
    }
    drop(lease.take());
    drop(writer);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn begin(tracking: &mut RpcTracking, value: Value) -> String {
        let prepared = tracking
            .prepare(&value, "test-connection")
            .unwrap()
            .unwrap();
        let key = prepared.operation_key.clone();
        tracking.insert(prepared);
        key
    }

    #[test]
    fn process_start_response_does_not_release_running_process() {
        let mut tracking = RpcTracking::default();
        let key = begin(
            &mut tracking,
            json!({"id": 1, "method": "process/start", "params": {"processId": "p"}}),
        );
        assert_eq!(
            tracking.guest_message(&json!({"id": 1, "result": {"processId": "p"}})),
            Some((key, "completed"))
        );
        assert!(!tracking.idle());
        tracking.guest_message(&json!({"method": "process/output", "params": {"processId": "p"}}));
        assert!(!tracking.idle());
        tracking.guest_message(&json!({"method": "process/exited", "params": {"processId": "p"}}));
        assert!(tracking.idle());
    }

    #[test]
    fn failed_process_start_releases_process_tracking() {
        let mut tracking = RpcTracking::default();
        let key = begin(
            &mut tracking,
            json!({"id": "start", "method": "process/start", "params": {"processId": "p"}}),
        );
        assert_eq!(
            tracking.guest_message(&json!({"id": "start", "error": {"code": -1}})),
            Some((key, "error"))
        );
        assert!(tracking.idle());
    }

    #[test]
    fn duplicate_ids_cannot_replace_an_existing_mutation() {
        let mut tracking = RpcTracking::default();
        let request = json!({"id": 2, "method": "fs/writeFile", "params": {}});
        let key = begin(&mut tracking, request.clone());
        assert!(tracking.prepare(&request, "test").is_err());
        assert_eq!(tracking.pending.len(), 1);
        assert_eq!(tracking.pending.values().next().unwrap().operation_key, key);
    }

    #[test]
    fn reused_rpc_id_gets_a_new_durable_operation_key() {
        let mut tracking = RpcTracking::default();
        let request = json!({"id": 2, "method": "fs/writeFile", "params": {}});
        let first = begin(&mut tracking, request.clone());
        tracking.guest_message(&json!({"id": 2, "result": {}}));
        let second = begin(&mut tracking, request);
        assert_ne!(first, second);
    }

    #[test]
    fn guest_callback_with_same_id_cannot_complete_a_client_request() {
        let mut tracking = RpcTracking::default();
        let key = begin(&mut tracking, json!({"id": 3, "method": "fs/writeFile"}));
        assert!(
            tracking
                .guest_message(&json!({"id": 3, "method": "approval/request", "params": {}}))
                .is_none()
        );
        assert_eq!(tracking.pending.len(), 1);
        assert_eq!(
            tracking.guest_message(&json!({"id": 3, "result": {}})),
            Some((key, "completed"))
        );
        assert!(!tracking.idle());
        tracking.harness_response(&json!({"id": 3, "result": {"approved": true}}));
        assert!(tracking.idle());
    }

    #[test]
    fn only_initialized_health_requests_get_local_replies() {
        let mut tracking = RpcTracking::default();
        let status = json!({"id": "probe", "method": "environment/status"});
        assert!(tracking.local_health_reply(&status).is_none());
        begin(&mut tracking, json!({"id": 1, "method": "initialize"}));
        tracking.guest_message(&json!({"id": 1, "result": {"sessionId": "s", "environmentInfo": {"executorVersion": "0.159.3"}}}));
        assert_eq!(
            tracking.local_health_reply(&status),
            Some(json!({"id": "probe", "result": {"status": "ready"}}))
        );
        assert_eq!(
            tracking.local_health_reply(&json!({"id": 4, "method": "environment/info"})),
            Some(json!({"id": 4, "result": {"executorVersion": "0.159.3"}}))
        );
        assert!(tracking.idle());
    }

    #[test]
    fn integer_and_string_rpc_ids_are_distinct() {
        let mut tracking = RpcTracking::default();
        begin(&mut tracking, json!({"id": 1, "method": "fs/readFile"}));
        begin(&mut tracking, json!({"id": "1", "method": "fs/readFile"}));
        tracking.guest_message(&json!({"id": 1, "result": {}}));
        assert_eq!(tracking.pending.len(), 1);
        assert!(!tracking.idle());
        tracking.guest_message(&json!({"id": "1", "result": {}}));
        assert!(tracking.idle());
    }

    #[test]
    fn duplicate_active_process_ids_and_missing_ids_are_rejected() {
        let mut tracking = RpcTracking::default();
        begin(
            &mut tracking,
            json!({"id": 1, "method": "process/start", "params": {"processId": "p"}}),
        );
        tracking.guest_message(&json!({"id": 1, "result": {}}));
        assert!(
            tracking
                .prepare(
                    &json!({"id": 2, "method": "process/start", "params": {"processId": "p"}}),
                    "test"
                )
                .is_err()
        );
        assert!(
            tracking
                .prepare(
                    &json!({"id": 2, "method": "process/start", "params": {}}),
                    "test"
                )
                .is_err()
        );
        assert!(
            tracking
                .prepare(
                    &json!({"method": "process/start", "params": {"processId": "q"}}),
                    "test"
                )
                .is_err()
        );
    }

    #[test]
    fn read_response_can_confirm_an_exit_without_the_notification() {
        let mut tracking = RpcTracking::default();
        begin(
            &mut tracking,
            json!({"id": 1, "method": "process/start", "params": {"processId": "p"}}),
        );
        tracking.guest_message(&json!({"id": 1, "result": {}}));
        begin(
            &mut tracking,
            json!({"id": 2, "method": "process/read", "params": {"processId": "p"}}),
        );
        tracking.guest_message(&json!({"id": 2, "result": {"exited": true}}));
        assert!(tracking.idle());
    }
}
