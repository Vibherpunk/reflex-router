pub mod models;
pub mod catalog;
pub mod solver;
pub mod proxy;
pub mod config;

use axum::{
    routing::post,
    Router, Json,
    extract::State,
    response::IntoResponse,
};
use clap::Parser;
use std::sync::Arc;
use tokio::net::TcpListener;

#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    #[arg(short, long, default_value_t = 8787)]
    port: u16,
}

#[derive(Clone)]
struct AppState {
    catalog: Arc<catalog::LiveCatalog>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    
    if args.port != 8787 {
        panic!("Port 8000 is forbidden. Must use 8787");
    }

    let live_catalog = Arc::new(catalog::LiveCatalog::new());
    
    // Seed immediately for immediate readiness
    live_catalog.refresh_catalog().await;
    
    let poll_catalog = live_catalog.clone();
    tokio::spawn(async move {
        poll_catalog.background_poll().await;
    });

    let state = AppState {
        catalog: live_catalog,
    };

    let app = Router::new()
        .route("/v1/chat/completions", post(chat_endpoint))
        .with_state(state);

    let addr = format!("0.0.0.0:{}", args.port);
    let listener = TcpListener::bind(&addr).await?;
    tracing::info!("Listening on {}", addr);
    
    axum::serve(listener, app).await?;
    
    Ok(())
}

async fn chat_endpoint(
    State(state): State<AppState>,
    Json(payload): Json<models::ChatRequest>,
) -> impl IntoResponse {
    let _decision = solver::resolve_route(&state.catalog, payload.tools_required, payload.reasoning_required, payload.tier);
    proxy::proxy_stream(payload).await.into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_port_invariant() {
        let args = Args { port: 8787 };
        assert_eq!(args.port, 8787);
    }
}
