use axum::{
    http::{StatusCode, HeaderMap, header},
    response::sse::{Event, Sse},
};
use futures::stream::Stream;
use reqwest::Client;
use std::convert::Infallible;
use futures::StreamExt;
use crate::models::{ChatRequest, RoutingDecision};

pub async fn proxy_stream(req: ChatRequest, headers: HeaderMap, decision: RoutingDecision) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, StatusCode> {
    let client = Client::new();
    
    // Determine provider url and token.
    let provider = decision.primary_provider.as_str();
    
    let simulated_url = req.extra.get("upstream_url")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
        
    let upstream_url = simulated_url.unwrap_or_else(|| {
        match provider {
            "opencode-go" => "https://opencode.ai/zen/go/v1/chat/completions".to_string(),
            _ => "https://openrouter.ai/api/v1/chat/completions".to_string()
        }
    });

    // Check for BYOK / multi-tenant
    let client_token = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok());
    
    let mut builder = client.post(&upstream_url).json(&req);

    if let Some(token) = client_token {
        builder = builder.header(header::AUTHORIZATION, token);
    } else {
        // Fallback to host default
        let host_key = match provider {
            "opencode-go" => std::env::var("OPENCODE_GO_API_KEY").unwrap_or_default(),
            _ => std::env::var("OPENROUTER_API_KEY").unwrap_or_default()
        };
        if !host_key.is_empty() {
            builder = builder.bearer_auth(host_key);
        }
    }
    
    // Forward X-Tenant-Id if present
    if let Some(tenant) = headers.get("X-Tenant-Id") {
        builder = builder.header("X-Tenant-Id", tenant);
    }

    let res = builder.send().await;
    
    match res {
        Ok(res) if res.status().is_success() => {
            let stream = res.bytes_stream().map(|chunk| {
                match chunk {
                    Ok(bytes) => {
                        let text = String::from_utf8_lossy(&bytes);
                        Ok(Event::default().data(text))
                    }
                    Err(_) => Ok(Event::default().data("[DONE]")),
                }
            });
            Ok(Sse::new(stream))
        }
        _ => {
            // Early failover if not 200 OK
            Err(StatusCode::BAD_GATEWAY)
        }
    }
}
