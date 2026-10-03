//! Embedded administrative dashboard. Observation never wakes a guest.
use crate::manager::Manager;
use anyhow::Result;
use axum::{
    Json, Router,
    extract::{Path, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, uri::Authority},
    middleware::{self, Next},
    response::{
        Html, IntoResponse, Response, Sse,
        sse::{Event, KeepAlive},
    },
    routing::get,
};
use futures_util::stream;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    convert::Infallible,
    env, fs,
    net::SocketAddr,
    str::FromStr,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::watch;

const INTERVAL: Duration = Duration::from_secs(2);
const HISTORY: usize = 180;
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";

pub struct Config {
    pub address: SocketAddr,
    hosts: Vec<String>,
}
impl Config {
    pub fn from_env() -> Result<Option<Self>> {
        let address = env::var("ENVIRONMENT_WEB_ADDR").unwrap_or_else(|_| "127.0.0.1:6091".into());
        if address == "off" {
            return Ok(None);
        }
        let address: SocketAddr = address.parse()?;
        let mut hosts = vec!["localhost".into(), "127.0.0.1".into(), "::1".into()];
        if !address.ip().is_unspecified() {
            hosts.push(address.ip().to_string());
        }
        hosts.extend(
            env::var("ENVIRONMENT_WEB_HOSTS")
                .unwrap_or_default()
                .split(',')
                .map(str::trim)
                .filter(|host| !host.is_empty())
                .map(|host| host.to_ascii_lowercase()),
        );
        Ok(Some(Self { address, hosts }))
    }
}

struct Collector {
    start: Instant,
    previous: Option<(Instant, u64)>,
    history: VecDeque<Value>,
}
pub struct Web {
    manager: Arc<Manager>,
    paperclip: Arc<crate::paperclip::Catalog>,
    hosts: Vec<String>,
    hostname: String,
    collector: Mutex<Collector>,
    latest: watch::Sender<Arc<Value>>,
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}
fn keyed_number(contents: &str, key: &str) -> Option<u64> {
    contents
        .lines()
        .find(|line| line.starts_with(key))
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|number| number.parse().ok())
}
fn cpu_ticks(contents: &str) -> Option<u64> {
    let fields = contents
        .rsplit_once(')')?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    Some(fields.get(11)?.parse::<u64>().ok()? + fields.get(12)?.parse::<u64>().ok()?)
}

impl Web {
    pub fn new(
        manager: Arc<Manager>,
        paperclip: Arc<crate::paperclip::Catalog>,
        config: &Config,
    ) -> Arc<Self> {
        let (latest, _) = watch::channel(Arc::new(json!({})));
        let web = Arc::new(Self {
            manager,
            paperclip,
            hosts: config.hosts.clone(),
            hostname: fs::read_to_string("/proc/sys/kernel/hostname")
                .unwrap_or_else(|_| "host".into())
                .trim()
                .into(),
            collector: Mutex::new(Collector {
                start: Instant::now(),
                previous: None,
                history: VecDeque::new(),
            }),
            latest,
        });
        web.refresh();
        web
    }

    fn refresh(&self) {
        let mut collector = self.collector.lock().unwrap();
        let mut data = self.manager.observation();
        data["paperclip"] = self.paperclip.snapshot();
        let meminfo = fs::read_to_string("/proc/meminfo").unwrap_or_default();
        let available = keyed_number(&meminfo, "MemAvailable:").unwrap_or(0) * 1024;
        let total = keyed_number(&meminfo, "MemTotal:").unwrap_or(0) * 1024;
        let status = fs::read_to_string("/proc/self/status").unwrap_or_default();
        let rss = keyed_number(&status, "VmRSS:").unwrap_or(0) * 1024;
        let pss = fs::read_to_string("/proc/self/smaps_rollup")
            .ok()
            .and_then(|text| keyed_number(&text, "Pss:"))
            .map(|value| value * 1024);
        let instant = Instant::now();
        let ticks = fs::read_to_string("/proc/self/stat")
            .ok()
            .and_then(|text| cpu_ticks(&text));
        let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as f64;
        let cpu = ticks.and_then(|ticks| {
            collector.previous.map(|(time, previous)| {
                ticks.saturating_sub(previous) as f64
                    / hz
                    / instant.duration_since(time).as_secs_f64().max(0.001)
                    * 100.0
            })
        });
        if let Some(ticks) = ticks {
            collector.previous = Some((instant, ticks));
        }
        let rows = data["workspaces"].as_array().unwrap();
        let awake = rows.iter().filter(|row| row["pid"].is_u64()).count();
        let leases: u64 = rows
            .iter()
            .filter_map(|row| row["active_operations"].as_u64())
            .sum();
        let queued: u64 = rows
            .iter()
            .filter_map(|row| row["queued_operations"].as_u64())
            .sum();
        let guest_rss: u64 = rows
            .iter()
            .filter_map(|row| row["rss_bytes"].as_u64())
            .sum();
        let timestamp = now();
        // Control actions can refresh the table without adding extra history points.
        if collector
            .history
            .back()
            .and_then(|point| point[0].as_f64())
            .is_none_or(|last| timestamp - last >= 1.5)
        {
            collector.history.push_back(json!([
                timestamp, awake, leases, queued, pss, guest_rss, available
            ]));
            if collector.history.len() > HISTORY {
                collector.history.pop_front();
            }
        }
        data["sampled_at"] = json!(timestamp);
        data["hostname"] = json!(self.hostname);
        data["version"] = json!(env!("CARGO_PKG_VERSION"));
        data["interval_seconds"] = json!(INTERVAL.as_secs());
        data["uptime_seconds"] = json!(collector.start.elapsed().as_secs_f64());
        data["service"] =
            json!({"pid":std::process::id(),"pss_bytes":pss,"rss_bytes":rss,"cpu_percent":cpu});
        data["host"] = json!({"available_bytes":available,"total_bytes":total});
        data["history"] = json!(collector.history);
        self.latest.send_replace(Arc::new(data));
    }

    pub async fn sample(self: Arc<Self>) {
        loop {
            tokio::select! { _ = self.manager.shutdown_token.cancelled() => break, _ = tokio::time::sleep(INTERVAL) => {} }
            self.refresh();
        }
    }
}

fn headers_allowed(headers: &HeaderMap, hosts: &[String], mutation: bool) -> bool {
    let Some(host) = headers.get("host").and_then(|value| value.to_str().ok()) else {
        return false;
    };
    if host.contains('@') {
        return false;
    }
    let Ok(authority) = Authority::from_str(host) else {
        return false;
    };
    let hostname = authority
        .host()
        .trim_matches(['[', ']'])
        .to_ascii_lowercase();
    if !hosts.iter().any(|allowed| allowed == &hostname) {
        return false;
    }
    if headers
        .get("sec-fetch-site")
        .is_some_and(|value| value == "cross-site")
        && mutation
    {
        return false;
    }
    if !mutation {
        return true;
    }
    headers
        .get("x-environment-ui")
        .is_some_and(|value| value == "1")
        && headers
            .get("origin")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|origin| {
                origin == format!("http://{host}") || origin == format!("https://{host}")
            })
}

async fn protect(State(web): State<Arc<Web>>, request: Request, next: Next) -> Response {
    let mutation = !matches!(*request.method(), Method::GET | Method::HEAD);
    let mut response = if headers_allowed(request.headers(), &web.hosts, mutation) {
        next.run(request).await
    } else {
        crate::error(
            StatusCode::FORBIDDEN,
            "Use the dashboard's configured host and same-origin controls",
        )
    };
    let headers = response.headers_mut();
    headers.insert("content-security-policy", HeaderValue::from_static(CSP));
    headers.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    headers.insert("x-frame-options", HeaderValue::from_static("DENY"));
    headers.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    headers.insert("cache-control", HeaderValue::from_static("no-store"));
    response
}

async fn dashboard(State(web): State<Arc<Web>>) -> Json<Value> {
    Json((**web.latest.borrow()).clone())
}

async fn events(State(web): State<Arc<Web>>) -> impl IntoResponse {
    let receiver = web.latest.subscribe();
    let cancel = web.manager.shutdown_token.clone();
    let stream = stream::unfold(
        (receiver, cancel, true),
        |(mut receiver, cancel, first)| async move {
            if !first {
                tokio::select! { _ = cancel.cancelled() => return None, changed = receiver.changed() => { if changed.is_err() { return None; } } }
            } else if cancel.is_cancelled() {
                return None;
            }
            let data = receiver.borrow_and_update().to_string();
            Some((
                Ok::<_, Infallible>(Event::default().event("snapshot").data(data)),
                (receiver, cancel, false),
            ))
        },
    );
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

async fn create(
    State(web): State<Arc<Web>>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let id = match body
        .ok()
        .and_then(|Json(value)| value.get("id").and_then(Value::as_str).map(str::to_owned))
    {
        Some(id) if crate::manager::valid_id(&id) => id,
        _ => {
            return crate::error(
                StatusCode::BAD_REQUEST,
                "Use a workspace ID with 1 to 48 letters, digits, underscores, or hyphens",
            );
        }
    };
    match web.manager.allocate(&id) {
        Ok(mut binding) => {
            binding.as_object_mut().unwrap().remove("auth_bearer_token");
            web.refresh();
            Json(binding).into_response()
        }
        Err(error) => crate::error(StatusCode::CONFLICT, error),
    }
}

async fn action(
    State(web): State<Arc<Web>>,
    Path((id, action)): Path<(String, String)>,
) -> Response {
    if web.manager.binding(&id).is_err() {
        return crate::error(StatusCode::NOT_FOUND, "Workspace does not exist");
    }
    if !matches!(
        action.as_str(),
        "resume" | "suspend" | "shutdown" | "recover"
    ) {
        return crate::error(StatusCode::NOT_FOUND, "Unknown action");
    }
    let key = uuid::Uuid::new_v4().to_string();
    if let Err(error) = web
        .manager
        .operation_begin(&key, &id, &format!("workspace/{action}"))
    {
        return crate::error(StatusCode::INTERNAL_SERVER_ERROR, error);
    }
    web.refresh();
    let result = web.manager.admin_action(&id, &action).await;
    if let Err(error) = web
        .manager
        .operation_finish(&key, if result.is_ok() { "completed" } else { "error" })
    {
        tracing::error!(%error,"Could not journal a dashboard action");
    }
    web.refresh();
    match result {
        Ok(value) => Json(value).into_response(),
        Err(error) => crate::error(StatusCode::CONFLICT, error),
    }
}

pub fn router(web: Arc<Web>) -> Router {
    Router::new()
        .route(
            "/",
            get(|| async { Html(include_str!("../web/index.html")) }),
        )
        .route(
            "/app.css",
            get(|| async {
                (
                    [("content-type", "text/css; charset=utf-8")],
                    include_str!("../web/app.css"),
                )
            }),
        )
        .route(
            "/app.js",
            get(|| async {
                (
                    [("content-type", "text/javascript; charset=utf-8")],
                    include_str!("../web/app.js"),
                )
            }),
        )
        .route("/api/dashboard", get(dashboard))
        .route("/api/workspaces", axum::routing::post(create))
        .route("/api/workspaces/{id}/{action}", axum::routing::post(action))
        .route("/events", get(events))
        .layer(middleware::from_fn_with_state(web.clone(), protect))
        .with_state(web)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cross_site_controls_and_rebound_hosts_are_rejected() {
        let hosts = vec!["localhost".into(), "127.0.0.1".into(), "::1".into()];
        let mut headers = HeaderMap::new();
        headers.insert("host", "localhost:6091".parse().unwrap());
        assert!(headers_allowed(&headers, &hosts, false));
        assert!(!headers_allowed(&headers, &hosts, true));
        headers.insert("origin", "http://localhost:6091".parse().unwrap());
        headers.insert("x-environment-ui", "1".parse().unwrap());
        assert!(headers_allowed(&headers, &hosts, true));
        headers.insert("origin", "https://untrusted.example".parse().unwrap());
        assert!(!headers_allowed(&headers, &hosts, true));
        headers.insert("host", "untrusted.example:6091".parse().unwrap());
        assert!(!headers_allowed(&headers, &hosts, false));
        headers.insert("host", "user@localhost:6091".parse().unwrap());
        assert!(!headers_allowed(&headers, &hosts, false));
        headers.insert("host", "[::1]:6091".parse().unwrap());
        assert!(headers_allowed(&headers, &hosts, false));
    }
    #[test]
    fn proc_stat_parser_handles_spaces_in_process_names() {
        assert_eq!(
            cpu_ticks("12 (test process) S 1 2 3 4 5 6 7 8 9 10 25 30"),
            Some(55)
        );
        assert_eq!(
            keyed_number("Pss: 123 kB\nPss_Anon: 8 kB", "Pss:"),
            Some(123)
        );
        assert_eq!(cpu_ticks("incomplete"), None);
    }
}
