use axum::{
    http::StatusCode,
    response::sse::{Event, Sse},
};
use futures::stream::Stream;
use reqwest::Client;
use std::convert::Infallible;
use futures::StreamExt;

pub async fn proxy_stream(req: crate::models::ChatRequest) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, StatusCode> {
    let client = Client::new();
    
    // Extract upstream URL from extra or default to a dummy for tests
    let simulated_url = req.extra.get("upstream_url")
        .and_then(|v| v.as_str())
        .unwrap_or("http://127.0.0.1:9999/v1/chat/completions");
        
    let res = client.post(simulated_url).json(&req).send().await;
    
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
