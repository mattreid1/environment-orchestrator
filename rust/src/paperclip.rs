//! Read-only Paperclip discovery. Agent records do not reserve guest slots.
use crate::manager::Manager;
use anyhow::{Result, bail};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use reqwest::{
    Client, Url,
    header::{COOKIE, HeaderValue, SET_COOKIE},
};
use rusqlite::{Connection, OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path as FsPath, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio_util::sync::CancellationToken;

const MAX_RESPONSE: usize = 2 * 1024 * 1024;

#[derive(Clone, Deserialize)]
pub struct Company {
    id: String,
    name: String,
    prefix: String,
}
#[derive(Deserialize)]
struct Config {
    base_url: String,
    companies: Vec<Company>,
    login_file: PathBuf,
}
#[derive(Deserialize)]
struct Login {
    email: String,
    password: String,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Agent {
    id: String,
    company_id: String,
    name: String,
    status: String,
    adapter_type: String,
    #[serde(default)]
    role: String,
    #[serde(default)]
    adapter_config: Value,
    #[serde(default)]
    metadata: Value,
}

pub struct Catalog {
    db: Mutex<Connection>,
    status: Mutex<Value>,
    config_path: PathBuf,
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}
fn valid_remote_id(id: &str) -> bool {
    uuid::Uuid::parse_str(id).is_ok()
}
fn private_json(path: &FsPath) -> Result<Value> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o077 != 0
        || metadata.len() > 16384
    {
        bail!("Paperclip configuration and login files must be owned regular files with mode 0600");
    }
    Ok(serde_json::from_slice(&fs::read(path)?)?)
}
fn parse_config(value: Value) -> Result<Config> {
    let config: Config = serde_json::from_value(value)?;
    let url = Url::parse(&config.base_url)?;
    let loopback = matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    if !(url.scheme() == "https" || url.scheme() == "http" && loopback)
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
        || config.companies.is_empty()
        || config.companies.len() > 100
    {
        bail!("Paperclip requires an HTTPS origin and a company allowlist");
    }
    let mut ids = std::collections::HashSet::new();
    for company in &config.companies {
        if !valid_remote_id(&company.id)
            || !ids.insert(&company.id)
            || company.name.is_empty()
            || company.name.len() > 256
            || company.prefix.is_empty()
            || company.prefix.len() > 32
            || !company
                .prefix
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
        {
            bail!("Paperclip company configuration is invalid");
        }
    }
    Ok(config)
}
async fn response_json(mut response: reqwest::Response) -> Result<Value> {
    if !response.status().is_success() {
        bail!(
            "Paperclip request failed (HTTP {})",
            response.status().as_u16()
        );
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        if bytes.len() + chunk.len() > MAX_RESPONSE {
            bail!("Paperclip response exceeds the size limit");
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(serde_json::from_slice(&bytes)?)
}
async fn login(client: &Client, config: &Config) -> Result<HeaderValue> {
    let login: Login = serde_json::from_value(private_json(&config.login_file)?)?;
    if login.email.is_empty() || login.password.is_empty() {
        bail!("Paperclip login is empty");
    }
    let response = client
        .post(format!(
            "{}/api/auth/sign-in/email",
            config.base_url.trim_end_matches('/')
        ))
        .header("Origin", config.base_url.trim_end_matches('/'))
        .json(&json!({"email":login.email,"password":login.password}))
        .send()
        .await?;
    if !response.status().is_success() {
        bail!(
            "Paperclip login failed (HTTP {})",
            response.status().as_u16()
        );
    }
    let cookie = response
        .headers()
        .get_all(SET_COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .filter_map(|value| value.split(';').next())
        .find(|value| {
            value.split('=').next().is_some_and(|name| {
                let name = name.strip_prefix("__Secure-").unwrap_or(name);
                name == "better-auth.session_token"
                    || name.starts_with("paperclip-") && name.ends_with(".session_token")
            })
        })
        .ok_or_else(|| anyhow::anyhow!("Paperclip did not issue a session cookie"))?;
    let mut header = HeaderValue::from_str(cookie)?;
    header.set_sensitive(true);
    // Login response bodies contain tokens. Do not retain or print them.
    Ok(header)
}

fn recognized_profile(value: &Value) -> Option<&str> {
    value.as_str().filter(|profile| {
        matches!(
            *profile,
            "swe" | "frontend" | "marketing" | "sales" | "research"
        )
    })
}

fn agent_profile(agent: &Agent) -> String {
    // Paperclip redacts adapter env values in discovery responses. Public
    // metadata lets discovery select the same profile as the launcher's env.
    if let Some(profile) = recognized_profile(&agent.metadata["environmentProfile"]) {
        return profile.to_string();
    }
    if let Some(binding) = agent.adapter_config.pointer("/env/ENVIRONMENT_PROFILE") {
        let value = if binding["type"] == "plain" {
            &binding["value"]
        } else {
            binding
        };
        if let Some(profile) = recognized_profile(value) {
            return profile.to_string();
        }
    }
    match agent.role.to_ascii_lowercase().as_str() {
        "marketing" | "marketer" | "cmo" => "marketing",
        "sales" | "salesperson" | "cro" => "sales",
        "research" | "researcher" | "analyst" => "research",
        _ if agent.adapter_type == "claude_local" => "frontend",
        _ => "swe",
    }
    .to_string()
}

impl Catalog {
    async fn discover(&self, company: &str) -> Result<()> {
        let config = parse_config(private_json(&self.config_path)?)?;
        if !config.companies.iter().any(|item| item.id == company) {
            bail!("Paperclip company is not enabled on this host");
        }
        let client = Client::builder()
            .timeout(Duration::from_secs(15))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        let session = login(&client, &config).await?;
        let mut companies = Vec::new();
        for company in &config.companies {
            let response = client
                .get(format!(
                    "{}/api/companies/{}/agents",
                    config.base_url.trim_end_matches('/'),
                    company.id
                ))
                .header(COOKIE, session.clone())
                .send()
                .await?;
            companies.push((
                company.clone(),
                serde_json::from_value(response_json(response).await?)?,
            ));
        }
        self.replace(&config, companies)
    }

    fn available(&self, company: &str, agent: &str) -> Result<bool> {
        Ok(self
            .db
            .lock()
            .unwrap()
            .query_row(
                "SELECT 1 FROM agents WHERE company=? AND id=? AND present=1 AND status IN ('idle','running','error')",
                params![company, agent],
                |_| Ok(()),
            )
            .optional()?
            .is_some())
    }
    async fn refresh_if_unavailable(&self, company: &str, agent: &str) -> Result<()> {
        // A pause, deletion, or resume can race the periodic discovery poll.
        // Refresh unavailable cached records before refusing a new execution.
        if !self.available(company, agent)? {
            self.discover(company).await?;
        }
        Ok(())
    }

    pub fn new(state: &FsPath) -> Result<Arc<Self>> {
        let db = Connection::open(state.join("paperclip.sqlite"))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "synchronous", "FULL")?;
        db.execute_batch(
            "CREATE TABLE IF NOT EXISTS agents (
            company TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
            adapter TEXT NOT NULL, company_name TEXT NOT NULL, prefix TEXT NOT NULL,
            origin TEXT NOT NULL, present INTEGER NOT NULL, workspace TEXT UNIQUE,
            PRIMARY KEY(company,id));",
        )?;
        let columns = db
            .prepare("PRAGMA table_info(agents)")?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        if !columns.iter().any(|column| column == "profile") {
            db.execute(
                "ALTER TABLE agents ADD COLUMN profile TEXT NOT NULL DEFAULT 'swe'",
                [],
            )?;
        }
        let config_path = state.join("paperclip.json");
        Ok(Arc::new(Self {
            db: Mutex::new(db),
            status: Mutex::new(
                json!({"configured":config_path.exists(),"connected":false,"last_synced":null,"error":null}),
            ),
            config_path,
        }))
    }

    pub fn snapshot(&self) -> Value {
        let db = self.db.lock().unwrap();
        let rows = (|| -> Result<Vec<Value>> {
            let mut statement = db.prepare_cached("SELECT company,id,name,status,adapter,company_name,prefix,origin,present,workspace,profile FROM agents ORDER BY present DESC,company_name,name,id")?;
            Ok(statement.query_map([], |row| {
                let origin: String = row.get(7)?;
                let prefix: String = row.get(6)?;
                let id: String = row.get(1)?;
                Ok(json!({"company_id":row.get::<_,String>(0)?,"id":id,"name":row.get::<_,String>(2)?,
                    "status":row.get::<_,String>(3)?,"adapter_type":row.get::<_,String>(4)?,"company_name":row.get::<_,String>(5)?,
                    "profile":row.get::<_,String>(10)?,"present":row.get::<_,bool>(8)?,"workspace_id":row.get::<_,Option<String>>(9)?,
                    "url":format!("{origin}/{prefix}/agents/{id}")}))
            })?.collect::<rusqlite::Result<Vec<_>>>()?)
        })();
        let mut status = self.status.lock().unwrap().clone();
        status["agents"] = json!(rows.unwrap_or_default());
        status
    }

    fn replace(&self, config: &Config, companies: Vec<(Company, Vec<Agent>)>) -> Result<()> {
        // Validate the complete response before replacing any catalog entries.
        for (company, agents) in &companies {
            let mut ids = std::collections::HashSet::new();
            for agent in agents {
                if agent.company_id != company.id
                    || !valid_remote_id(&agent.id)
                    || !ids.insert(&agent.id)
                    || agent.name.is_empty()
                    || agent.name.len() > 256
                    || agent.status.len() > 64
                    || agent.adapter_type.len() > 128
                {
                    bail!("Paperclip returned invalid or cross-company agent records");
                }
            }
        }
        let mut db = self.db.lock().unwrap();
        let transaction = db.transaction()?;
        transaction.execute("UPDATE agents SET present=0", [])?;
        for (company, agents) in companies {
            for agent in agents {
                let profile = agent_profile(&agent);
                transaction.execute("INSERT INTO agents(company,id,name,status,adapter,company_name,prefix,origin,present,profile) VALUES(?,?,?,?,?,?,?,?,1,?)
                    ON CONFLICT(company,id) DO UPDATE SET name=excluded.name,status=excluded.status,adapter=excluded.adapter,
                    company_name=excluded.company_name,prefix=excluded.prefix,origin=excluded.origin,present=1,profile=excluded.profile",
                    params![company.id,agent.id,agent.name,agent.status,agent.adapter_type,company.name,company.prefix,config.base_url.trim_end_matches('/'),profile])?;
            }
        }
        transaction.commit()?;
        let mut status = self.status.lock().unwrap();
        status["connected"] = json!(true);
        status["last_synced"] = json!(now());
        status["error"] = Value::Null;
        Ok(())
    }

    fn workspace(&self, company: &str, agent: &str) -> Result<String> {
        if !valid_remote_id(company) || !valid_remote_id(agent) {
            bail!("Invalid Paperclip agent identity");
        }
        let db = self.db.lock().unwrap();
        let record: Option<(Option<String>, String, bool)> = db
            .query_row(
                "SELECT workspace,status,present FROM agents WHERE company=? AND id=?",
                params![company, agent],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let (workspace, status, present) =
            record.ok_or_else(|| anyhow::anyhow!("Paperclip agent has not been discovered"))?;
        if !present || !matches!(status.as_str(), "idle" | "running" | "error") {
            bail!("Paperclip agent is not available for execution");
        }
        if let Some(workspace) = workspace {
            return Ok(workspace);
        }
        let workspace = format!("pc-{}", uuid::Uuid::new_v4().simple());
        db.execute(
            "UPDATE agents SET workspace=? WHERE company=? AND id=?",
            params![workspace, company, agent],
        )?;
        Ok(workspace)
    }

    fn profile(&self, company: &str, agent: &str) -> Result<String> {
        Ok(self.db.lock().unwrap().query_row(
            "SELECT profile FROM agents WHERE company=? AND id=?",
            params![company, agent],
            |row| row.get(0),
        )?)
    }

    pub async fn run(self: Arc<Self>, cancel: CancellationToken) {
        let client = match Client::builder()
            .timeout(Duration::from_secs(15))
            .redirect(reqwest::redirect::Policy::none())
            .build()
        {
            Ok(client) => client,
            Err(_) => {
                self.status.lock().unwrap()["error"] =
                    json!("Paperclip HTTP client could not start");
                return;
            }
        };
        let mut session: Option<HeaderValue> = None;
        let mut previous_config = Value::Null;
        loop {
            if self.config_path.exists() {
                self.status.lock().unwrap()["configured"] = json!(true);
                let poll = async {
                    let value = private_json(&self.config_path)?;
                    let config = parse_config(value.clone())?;
                    if value != previous_config {
                        session = None;
                        previous_config = value;
                    }
                    if session.is_none() {
                        session = Some(login(&client, &config).await?);
                    }
                    let mut companies = Vec::new();
                    for company in &config.companies {
                        let response = client
                            .get(format!(
                                "{}/api/companies/{}/agents",
                                config.base_url.trim_end_matches('/'),
                                company.id
                            ))
                            .header(COOKIE, session.clone().unwrap())
                            .send()
                            .await?;
                        if matches!(response.status().as_u16(), 401 | 403) {
                            session = None;
                        }
                        let agents = serde_json::from_value(response_json(response).await?)?;
                        companies.push((company.clone(), agents));
                    }
                    self.replace(&config, companies)
                };
                let result =
                    tokio::select! { _ = cancel.cancelled() => return, result = poll => result };
                if result.is_err() {
                    let mut status = self.status.lock().unwrap();
                    status["connected"] = json!(false);
                    // Upstream bodies and parser errors can contain credentials.
                    status["error"] = json!(
                        "Paperclip sync failed. Check its availability, company access, and private login configuration."
                    );
                }
            } else {
                let mut status = self.status.lock().unwrap();
                status["configured"] = json!(false);
                status["connected"] = json!(false);
                session = None;
            }
            tokio::select! { _ = cancel.cancelled() => return, _ = tokio::time::sleep(Duration::from_secs(10)) => {} }
        }
    }
}

pub async fn bind(
    State((catalog, manager)): State<(Arc<Catalog>, Arc<Manager>)>,
    Path((company, agent)): Path<(String, String)>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    if !valid_remote_id(&company) || !valid_remote_id(&agent) {
        return crate::error(StatusCode::BAD_REQUEST, "Invalid Paperclip agent identity");
    }
    if catalog
        .refresh_if_unavailable(&company, &agent)
        .await
        .is_err()
    {
        return crate::error(
            StatusCode::CONFLICT,
            "Paperclip agent discovery failed. Check company access and private configuration.",
        );
    }
    let explicit = match body {
        Ok(Json(value)) => match value.get("profile") {
            Some(Value::String(profile)) => Some(profile.clone()),
            Some(_) => {
                return crate::error(
                    StatusCode::BAD_REQUEST,
                    "Workspace profile must be a string",
                );
            }
            None => None,
        },
        Err(_) => None,
    };
    let result = (|| -> Result<Value> {
        let workspace = catalog.workspace(&company, &agent)?;
        // Role/config changes do not replace an already allocated workspace.
        let profile = match explicit {
            Some(profile) => Some(profile),
            None if manager.binding(&workspace).is_ok() => None,
            None => Some(catalog.profile(&company, &agent)?),
        };
        manager.allocate_profile(&workspace, profile.as_deref())
    })();
    match result {
        Ok(binding) => Json(binding).into_response(),
        Err(error) => crate::error(StatusCode::CONFLICT, error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const COMPANY: &str = "8744dbdb-cdd7-4fee-8b46-3bd6ae6705fe";
    const AGENT: &str = "f3ccf705-bd33-4082-a266-3d06da1433b9";
    fn config() -> Config {
        parse_config(json!({"base_url":"https://example.test/","companies":[{"id":COMPANY,"name":"AIME","prefix":"AIME"}],"login_file":"/private/login.json"})).unwrap()
    }
    fn agent(name: &str) -> Agent {
        Agent {
            id: AGENT.into(),
            company_id: COMPANY.into(),
            name: name.into(),
            status: "idle".into(),
            adapter_type: "codex_local".into(),
            role: "engineer".into(),
            adapter_config: json!({}),
            metadata: json!({}),
        }
    }
    #[test]
    fn discovery_is_lazy_and_bindings_survive_rename_removal_and_restart() {
        let folder = tempfile::tempdir().unwrap();
        let catalog = Catalog::new(folder.path()).unwrap();
        let config = config();
        catalog
            .replace(
                &config,
                vec![(config.companies[0].clone(), vec![agent("Chief of Staff")])],
            )
            .unwrap();
        assert!(catalog.snapshot()["agents"][0]["workspace_id"].is_null());
        let workspace = catalog.workspace(COMPANY, AGENT).unwrap();
        catalog
            .replace(
                &config,
                vec![(config.companies[0].clone(), vec![agent("Renamed")])],
            )
            .unwrap();
        assert_eq!(catalog.workspace(COMPANY, AGENT).unwrap(), workspace);
        catalog.replace(&config, vec![]).unwrap();
        assert_eq!(catalog.snapshot()["agents"][0]["present"], false);
        assert!(catalog.workspace(COMPANY, AGENT).is_err());
        drop(catalog);
        let catalog = Catalog::new(folder.path()).unwrap();
        assert_eq!(catalog.snapshot()["agents"][0]["workspace_id"], workspace);
    }
    #[test]
    fn invalid_company_response_does_not_replace_previous_records() {
        let folder = tempfile::tempdir().unwrap();
        let catalog = Catalog::new(folder.path()).unwrap();
        let config = config();
        catalog
            .replace(
                &config,
                vec![(config.companies[0].clone(), vec![agent("Original")])],
            )
            .unwrap();
        let mut wrong = agent("Wrong");
        wrong.company_id = uuid::Uuid::new_v4().to_string();
        assert!(
            catalog
                .replace(&config, vec![(config.companies[0].clone(), vec![wrong])])
                .is_err()
        );
        assert_eq!(catalog.snapshot()["agents"][0]["name"], "Original");
    }
    #[test]
    fn credentials_cannot_use_cleartext_remote_origins_or_redirect_credentials() {
        for url in [
            "http://192.168.50.80",
            "https://user:password@example.test",
            "https://example.test/path",
            "https://example.test/?token=secret",
        ] {
            assert!(parse_config(json!({"base_url":url,"companies":[{"id":COMPANY,"name":"AIME","prefix":"AIME"}],"login_file":"/private/login.json"})).is_err());
        }
    }
    #[test]
    fn role_profiles_and_explicit_frontend_configuration() {
        let mut agent = agent("Worker");
        assert_eq!(agent_profile(&agent), "swe");
        for (role, profile) in [
            ("cmo", "marketing"),
            ("sales", "sales"),
            ("researcher", "research"),
        ] {
            agent.role = role.into();
            assert_eq!(agent_profile(&agent), profile);
        }
        agent.role = "engineer".into();
        agent.adapter_config = json!({"env":{"ENVIRONMENT_PROFILE":"frontend"}});
        assert_eq!(agent_profile(&agent), "frontend");
        agent.adapter_config =
            json!({"env":{"ENVIRONMENT_PROFILE":{"type":"plain","value":"frontend"}}});
        assert_eq!(agent_profile(&agent), "frontend");
    }
    #[test]
    fn discovery_selects_profiles_when_paperclip_redacts_adapter_environment() {
        let mut agent = agent("Frontend worker");
        agent.adapter_config = json!({"env":{"ENVIRONMENT_PROFILE":"***REDACTED***"}});
        agent.metadata = json!({"environmentProfile":"frontend"});
        assert_eq!(agent_profile(&agent), "frontend");
        // Public metadata is authoritative when a visible env value differs.
        agent.adapter_config = json!({"env":{"ENVIRONMENT_PROFILE":"swe"}});
        assert_eq!(agent_profile(&agent), "frontend");
        agent.metadata = Value::Null;
        agent.adapter_config =
            json!({"env":{"ENVIRONMENT_PROFILE":{"type":"plain","value":"***REDACTED***"}}});
        agent.adapter_type = "claude_local".into();
        assert_eq!(agent_profile(&agent), "frontend");
        agent.adapter_type = "codex_local".into();
        agent.role = "cmo".into();
        assert_eq!(agent_profile(&agent), "marketing");
        // Unknown and non-string values never become allocated profile names.
        for unknown in [
            json!("***REDACTED***"),
            json!("unknown"),
            json!(3),
            json!({"value":"frontend"}),
        ] {
            agent.metadata = json!({"environmentProfile":unknown.clone()});
            agent.adapter_config = json!({"env":{"ENVIRONMENT_PROFILE":unknown.clone()}});
            assert_eq!(agent_profile(&agent), "marketing");
            agent.role = "engineer".into();
            assert_eq!(agent_profile(&agent), "swe");
            agent.adapter_type = "claude_local".into();
            assert_eq!(agent_profile(&agent), "frontend");
            agent.adapter_type = "codex_local".into();
            agent.role = "cmo".into();
        }
        // Unsupported public metadata does not hide a recognized env value.
        agent.metadata = json!({"environmentProfile":"unknown"});
        agent.adapter_config = json!({"env":{"ENVIRONMENT_PROFILE":"research"}});
        assert_eq!(agent_profile(&agent), "research");
    }
    #[tokio::test]
    async fn on_demand_refresh_recovers_stale_pause_or_removal_and_keeps_binding() -> Result<()> {
        use axum::{
            Router,
            routing::{get, post},
        };
        use std::os::unix::fs::PermissionsExt;
        use std::sync::atomic::{AtomicUsize, Ordering};
        let rows = Arc::new(Mutex::new(
            json!([{"id":AGENT,"companyId":COMPANY,"name":"Worker","status":"idle","adapterType":"codex_local"}]),
        ));
        let logins = Arc::new(AtomicUsize::new(0));
        let login_count = logins.clone();
        let remote_rows = rows.clone();
        let router = Router::new()
            .route(
                "/api/auth/sign-in/email",
                post(move || {
                    let count = login_count.clone();
                    async move {
                        count.fetch_add(1, Ordering::SeqCst);
                        (
                            [("set-cookie", "paperclip-test.session_token=fixture; Path=/")],
                            Json(json!({"ok":true})),
                        )
                    }
                }),
            )
            .route(
                &format!("/api/companies/{COMPANY}/agents"),
                get(move || {
                    let rows = remote_rows.clone();
                    async move { Json(rows.lock().unwrap().clone()) }
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let server = tokio::spawn(async move { axum::serve(listener, router).await });
        let folder = tempfile::tempdir()?;
        let login_path = folder.path().join("login.json");
        fs::write(
            &login_path,
            serde_json::to_vec(&json!({"email":"test@example.test","password":"fixture"}))?,
        )?;
        fs::set_permissions(&login_path, fs::Permissions::from_mode(0o600))?;
        let value = json!({"base_url":format!("http://{address}"),"companies":[{"id":COMPANY,"name":"AIME","prefix":"AIME"}],"login_file":login_path});
        let config = parse_config(value.clone())?;
        let config_path = folder.path().join("paperclip.json");
        fs::write(&config_path, serde_json::to_vec(&value)?)?;
        fs::set_permissions(&config_path, fs::Permissions::from_mode(0o600))?;
        let catalog = Catalog::new(folder.path())?;
        assert!(!catalog.available(COMPANY, AGENT)?);
        catalog.replace(
            &config,
            vec![(config.companies[0].clone(), vec![agent("Worker")])],
        )?;
        let workspace = catalog.workspace(COMPANY, AGENT)?;
        let mut paused = agent("Worker");
        paused.status = "paused".into();
        catalog.replace(&config, vec![(config.companies[0].clone(), vec![paused])])?;
        assert!(!catalog.available(COMPANY, AGENT)?);
        assert!(catalog.workspace(COMPANY, AGENT).is_err());
        catalog.refresh_if_unavailable(COMPANY, AGENT).await?;
        assert!(catalog.available(COMPANY, AGENT)?);
        assert_eq!(catalog.workspace(COMPANY, AGENT)?, workspace);
        assert_eq!(logins.load(Ordering::SeqCst), 1);
        catalog.refresh_if_unavailable(COMPANY, AGENT).await?;
        assert_eq!(
            logins.load(Ordering::SeqCst),
            1,
            "Healthy cached agents must not log in on each binding"
        );
        catalog.replace(&config, vec![])?;
        catalog.refresh_if_unavailable(COMPANY, AGENT).await?;
        assert!(catalog.available(COMPANY, AGENT)?);
        assert_eq!(catalog.workspace(COMPANY, AGENT)?, workspace);
        assert_eq!(logins.load(Ordering::SeqCst), 2);
        // A fresh upstream pause must still refuse execution.
        let mut paused = agent("Worker");
        paused.status = "paused".into();
        catalog.replace(&config, vec![(config.companies[0].clone(), vec![paused])])?;
        (*rows.lock().unwrap())[0]["status"] = json!("paused");
        catalog.refresh_if_unavailable(COMPANY, AGENT).await?;
        assert!(!catalog.available(COMPANY, AGENT)?);
        assert!(catalog.workspace(COMPANY, AGENT).is_err());
        // A fresh upstream deletion must also refuse while retaining its files.
        *rows.lock().unwrap() = json!([]);
        catalog.refresh_if_unavailable(COMPANY, AGENT).await?;
        assert!(!catalog.available(COMPANY, AGENT)?);
        assert!(catalog.workspace(COMPANY, AGENT).is_err());
        assert_eq!(catalog.snapshot()["agents"][0]["workspace_id"], workspace);
        assert_eq!(logins.load(Ordering::SeqCst), 4);
        server.abort();
        Ok(())
    }
}
