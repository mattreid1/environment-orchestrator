mod manager;
mod proxy;
mod vm;
mod web;

use anyhow::{Context, Result, bail};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use fs2::FileExt;
use manager::{Manager, Settings};
use serde_json::{Value, json};
use std::{
    env,
    fs::{self, OpenOptions},
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

fn error(status: StatusCode, message: impl std::fmt::Display) -> Response {
    (status, Json(json!({"error":message.to_string()}))).into_response()
}

async fn list(State(manager): State<Arc<Manager>>) -> Response {
    match manager.list().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => error(StatusCode::CONFLICT, e),
    }
}

async fn create(
    State(manager): State<Arc<Manager>>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let id = match body {
        Ok(Json(v)) => match v.get("id").and_then(Value::as_str) {
            Some(id) if manager::valid_id(id) => id.to_owned(),
            _ => {
                return error(
                    StatusCode::BAD_REQUEST,
                    "Workspace ID must contain 1 to 48 letters, digits, underscores, or hyphens",
                );
            }
        },
        Err(_) => {
            return error(
                StatusCode::BAD_REQUEST,
                "Request body must be a JSON object",
            );
        }
    };
    match manager.allocate(&id) {
        Ok(v) => Json(v).into_response(),
        Err(e) => error(StatusCode::CONFLICT, e),
    }
}

async fn status(State(manager): State<Arc<Manager>>, Path(id): Path<String>) -> Response {
    if manager.binding(&id).is_err() {
        return error(StatusCode::NOT_FOUND, "Workspace does not exist");
    }
    match manager.status(&id).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => error(StatusCode::CONFLICT, e),
    }
}

async fn action(
    State(manager): State<Arc<Manager>>,
    Path((id, action)): Path<(String, String)>,
) -> Response {
    if manager.binding(&id).is_err() {
        return error(StatusCode::NOT_FOUND, "Workspace does not exist");
    }
    if !matches!(
        action.as_str(),
        "resume" | "suspend" | "shutdown" | "recover"
    ) {
        return error(StatusCode::NOT_FOUND, "Unknown action");
    }
    match manager.admin_action(&id, &action).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => error(StatusCode::CONFLICT, e),
    }
}

fn settings() -> Result<Settings> {
    let state = env::var_os("ENVIRONMENT_STATE")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("HOME").unwrap_or_default())
                .join(".local/share/environment-orchestrator")
        });
    let slots =
        serde_json::from_str(&env::var("ENVIRONMENT_SLOTS").unwrap_or_else(|_| "[]".into()))?;
    let idle: f64 = env::var("ENVIRONMENT_IDLE_SECONDS")
        .unwrap_or_else(|_| "120".into())
        .parse()?;
    if !idle.is_finite() || idle < 0.0 {
        bail!("ENVIRONMENT_IDLE_SECONDS must be finite and nonnegative");
    }
    Ok(Settings {
        state,
        slots,
        idle: Duration::try_from_secs_f64(idle)?,
        port: env::var("ENVIRONMENT_PORT")
            .unwrap_or_else(|_| "6090".into())
            .parse()?,
        reserve_mb: env::var("ENVIRONMENT_HOST_RESERVE_MB")
            .unwrap_or_else(|_| "3072".into())
            .parse()?,
    })
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    unsafe {
        libc::umask(0o077);
    }
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "environment_orchestrator=info".into()),
        )
        .init();
    let settings = settings()?;
    fs::create_dir_all(&settings.state)?;
    fs::set_permissions(&settings.state, fs::Permissions::from_mode(0o700))?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(settings.state.join("service.lock"))?;
    lock.try_lock_exclusive()
        .context("Another orchestrator owns this state directory")?;
    let socket_path = settings.state.join("control.sock");
    if socket_path.exists() {
        fs::remove_file(&socket_path)?;
    }
    let unix = tokio::net::UnixListener::bind(&socket_path)?;
    fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))?;
    let tcp = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, settings.port)).await?;
    let manager = Manager::new(settings)?;
    let web_config = web::Config::from_env()?;
    let web_listener = if let Some(config) = &web_config {
        Some(tokio::net::TcpListener::bind(config.address).await?)
    } else {
        None
    };
    let admin = Router::new()
        .route("/workspaces", get(list).post(create))
        .route("/workspaces/{id}", get(status))
        .route("/workspaces/{id}/{action}", post(action))
        .with_state(manager.clone());
    let gateway = Router::new()
        .route("/workspaces/{id}/exec", get(proxy::handler))
        .with_state(manager.clone());
    let cancel_admin = manager.shutdown_token.clone();
    let cancel_gateway = manager.shutdown_token.clone();
    let admin_task = tokio::spawn(async move {
        axum::serve(unix, admin)
            .with_graceful_shutdown(cancel_admin.cancelled_owned())
            .await
    });
    let gateway_task = tokio::spawn(async move {
        axum::serve(tcp, gateway)
            .with_graceful_shutdown(cancel_gateway.cancelled_owned())
            .await
    });
    let idle_task = tokio::spawn(manager.clone().idle_loop());
    let web_tasks = if let (Some(config), Some(listener)) = (web_config, web_listener) {
        let state = web::Web::new(manager.clone(), &config);
        let router = web::router(state.clone());
        let cancel = manager.shutdown_token.clone();
        tracing::info!(address=%config.address,"Dashboard is ready");
        Some((
            tokio::spawn(state.sample()),
            tokio::spawn(async move {
                axum::serve(listener, router)
                    .with_graceful_shutdown(cancel.cancelled_owned())
                    .await
            }),
        ))
    } else {
        None
    };
    tracing::info!("Rust environment orchestrator is ready");
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        _ = terminate.recv() => {},
        result = tokio::signal::ctrl_c() => { result?; },
        _ = manager.shutdown_token.cancelled() => {},
    }
    let result = manager.stop().await;
    manager.shutdown_token.cancel();
    idle_task.await?;
    // Upgraded WebSockets drain through Manager::stop, rather than HTTP shutdown.
    admin_task.await??;
    gateway_task.await??;
    if let Some((sampler, server)) = web_tasks {
        sampler.await?;
        server.await??;
    }
    let _ = fs::remove_file(socket_path);
    result
}
