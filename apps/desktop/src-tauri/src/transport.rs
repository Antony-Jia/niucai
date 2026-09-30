use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tauri::Emitter;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use url::Url;

#[derive(Clone)]
pub struct Credentials {
    pub base: String,
    pub token: String,
    pub computer_url: String,
}
pub fn validate_url(value: &str) -> Result<Url, String> {
    let url = Url::parse(value.trim()).map_err(|_| "请输入有效的服务器地址")?;
    let loopback = matches!(
        url.host_str(),
        Some("localhost" | "127.0.0.1" | "[::1]" | "::1")
    );
    if url.scheme() != "https" && !(url.scheme() == "http" && loopback) {
        return Err("公网连接必须使用 HTTPS；HTTP 仅允许本机调试".into());
    }
    if !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("连接地址不能包含凭据、查询参数或片段".into());
    }
    Ok(url)
}
pub fn api_url(base: &str, path: &str) -> Result<Url, String> {
    if !path.starts_with("/api/")
        || path.contains("..")
        || path.contains('\\')
        || path.contains('#')
        || path.contains('%')
    {
        return Err("无效的 Kernel API 路径".into());
    }
    let base = validate_url(base)?;
    if base.path() != "/" && !base.path().is_empty() {
        return Err("Kernel 地址只填写源站地址，不添加 /api 后缀".into());
    }
    let target = base.join(path).map_err(|_| "无效 API 路径")?;
    if target.origin() != base.origin() || !target.path().starts_with("/api/") {
        return Err("API 请求不能离开 Kernel 源站".into());
    }
    Ok(target)
}
pub fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(45))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "无法创建网络客户端".into())
}
pub async fn request(
    c: &Credentials,
    path: &str,
    method: &str,
    body: Value,
) -> Result<Value, String> {
    let url = api_url(&c.base, path)?;
    let http = client()?;
    let mut request = match method {
        "GET" => http.get(url),
        "POST" => http.post(url),
        _ => return Err("不支持的请求方法".into()),
    };
    if method == "POST" && !body.is_null() {
        request = request.json(&body);
    }
    if path == "/api/chat" {
        request = request.timeout(Duration::from_secs(210));
    }
    let response = request
        .bearer_auth(&c.token)
        .send()
        .await
        .map_err(|_| "无法连接 Kernel，请检查服务器地址与网络")?;
    let status = response.status();
    if status.as_u16() == 401 {
        return Err("API Token 无效或已过期，请重新连接".into());
    }
    let value: Value = response
        .json()
        .await
        .map_err(|_| "服务器未返回有效的 Kernel 数据")?;
    if !status.is_success() {
        let detail = value["detail"].as_str().unwrap_or("Kernel 请求失败");
        return Err(format!(
            "{}（{}）",
            detail.chars().take(300).collect::<String>(),
            status.as_u16()
        ));
    }
    Ok(value)
}
fn publish(app: &tauri::AppHandle, value: Value, after: &mut u64) {
    if let Some(id) = value["id"].as_u64() {
        if id > *after {
            *after = id;
            let _ = app.emit_to("main", "kernel-event", value);
        }
    }
}
pub async fn stream(app: tauri::AppHandle, c: Credentials, mut after: u64) {
    loop {
        let mut url = match api_url(&c.base, "/api/events") {
            Ok(url) => url,
            Err(_) => return,
        };
        let scheme = if url.scheme() == "https" { "wss" } else { "ws" };
        let _ = url.set_scheme(scheme);
        if let Ok(Ok((mut socket, _))) =
            tokio::time::timeout(Duration::from_secs(10), connect_async(url.as_str())).await
        {
            if socket
                .send(Message::Text(
                    json!({"token":c.token,"after":after}).to_string().into(),
                ))
                .await
                .is_ok()
            {
                let _ = app.emit_to("main", "kernel-stream", "WebSocket 同步");
                let mut ping = tokio::time::interval(Duration::from_secs(25));
                loop {
                    tokio::select! {
                        message = socket.next() => {
                            match message {
                                Some(Ok(Message::Text(text))) => { if let Ok(value) = serde_json::from_str(&text) { publish(&app, value, &mut after); } },
                                Some(Ok(Message::Close(Some(frame)))) if u16::from(frame.code) == 1008 => {
                                    let _ = app.emit_to("main", "kernel-stream", "认证失效"); return;
                                },
                                Some(Ok(Message::Ping(data))) => { let _ = socket.send(Message::Pong(data)).await; },
                                Some(Ok(Message::Pong(_))) => {},
                                _ => break,
                            }
                        },
                        _ = ping.tick() => { if socket.send(Message::Ping(Vec::new().into())).await.is_err() { break; } }
                    }
                }
            }
        }
        let _ = app.emit_to("main", "kernel-stream", "轮询同步");
        match request(
            &c,
            &format!("/api/events?after={after}&limit=200"),
            "GET",
            Value::Null,
        )
        .await
        {
            Ok(Value::Array(events)) => {
                for event in events {
                    publish(&app, event, &mut after);
                }
            }
            Err(message) if message.contains("Token") => {
                let _ = app.emit_to("main", "kernel-stream", "认证失效");
                return;
            }
            Err(_) => {
                let _ = app.emit_to("main", "kernel-stream", "等待重连");
            }
            _ => {}
        }
        tokio::time::sleep(Duration::from_secs(3)).await;
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn require_encrypted_remote_connection() {
        assert!(validate_url("http://example.com").is_err());
        assert!(validate_url("https://user:password@example.com").is_err());
        assert!(validate_url("https://example.com?token=secret").is_err());
        assert!(validate_url("https://example.com").is_ok());
        assert!(validate_url("http://127.0.0.1:8080").is_ok());
    }
    #[test]
    fn prevent_api_origin_escape() {
        for path in [
            "https://evil.example/api/tasks",
            "//evil.example/api/tasks",
            "/api/../settings",
            "/api/%2e%2e/secrets",
            "/healthz",
        ] {
            assert!(api_url("https://example.com", path).is_err());
        }
        assert_eq!(
            api_url("https://example.com", "/api/tasks?limit=200")
                .unwrap()
                .host_str(),
            Some("example.com")
        );
    }
}
