//! Dynamic Model Catalog for reflex-router-rs.
//! Provides zero-regex live model discovery from upstream /v1/models,
//! fallback to ~/.vibehard/model-cache.json, and 2026 frontier defaults.

use std::sync::Arc;
use dashmap::DashMap;
use std::time::Duration;
use crate::models::{ModelMetadata, ComputeTier};

#[derive(Clone)]
pub struct LiveCatalog {
    pub models: Arc<DashMap<String, ModelMetadata>>,
}

impl Default for LiveCatalog {
    fn default() -> Self {
        Self::new()
    }
}

impl LiveCatalog {
    pub fn new() -> Self {
        Self {
            models: Arc::new(DashMap::new()),
        }
    }

    pub async fn background_poll(&self) {
        let mut interval = tokio::time::interval(Duration::from_secs(60));
        loop {
            interval.tick().await;
            self.refresh_catalog().await;
        }
    }

    pub async fn refresh_catalog(&self) {
        let mut success = false;
        
        let client = reqwest::Client::new();
        let mut req = client.get("https://openrouter.ai/api/v1/models");
        
        if let Ok(key) = std::env::var("OPENROUTER_API_KEY") {
            req = req.bearer_auth(key);
        }

        if let Ok(resp) = req.send().await {
            if let Ok(json) = resp.json::<serde_json::Value>().await {
                if let Some(data) = json.get("data").and_then(|d| d.as_array()) {
                    for item in data {
                        if let (Some(id), Some(created)) = (
                            item.get("id").and_then(|i| i.as_str()),
                            item.get("created").and_then(|c| c.as_u64())
                        ) {
                            let context_length = item.get("context_length").and_then(|c| c.as_u64()).unwrap_or(8192) as u32;
                            let provider = id.split('/').next().unwrap_or("unknown").to_string();
                            
                            let tools_supported = item.get("architecture")
                                .and_then(|a| a.get("tools_supported"))
                                .and_then(|t| t.as_bool())
                                .or_else(|| item.get("tools_supported").and_then(|t| t.as_bool()))
                                .unwrap_or(true);
                                
                            let reasoning_supported = item.get("architecture")
                                .and_then(|a| a.get("reasoning_supported"))
                                .and_then(|r| r.as_bool())
                                .or_else(|| item.get("reasoning_supported").and_then(|r| r.as_bool()))
                                .unwrap_or(false);
                                
                            let tier = if reasoning_supported { ComputeTier::Reasoning } else { ComputeTier::Base };
                            
                            self.models.insert(id.to_string(), ModelMetadata {
                                id: id.to_string(),
                                provider,
                                created,
                                context_length,
                                tools_supported,
                                reasoning_supported,
                                tier,
                            });
                        }
                    }
                    if !self.models.is_empty() {
                        success = true;
                    }
                }
            }
        }
        
        if !success {
            let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/ai".to_string());
            let cache_path = format!("{}/.vibehard/model-cache.json", home);
            
            if let Ok(contents) = std::fs::read_to_string(cache_path) {
                if let Ok(cached_models) = serde_json::from_str::<Vec<ModelMetadata>>(&contents) {
                    for model in cached_models {
                        self.models.insert(model.id.clone(), model);
                    }
                    if !self.models.is_empty() {
                        success = true;
                    }
                }
            }
        }

        if !success && self.models.is_empty() {
            self.models.insert("anthropic/claude-opus-5.5".to_string(), ModelMetadata {
                id: "anthropic/claude-opus-5.5".to_string(),
                provider: "anthropic".to_string(),
                created: 1780000000,
                context_length: 200000,
                tools_supported: true,
                reasoning_supported: true,
                tier: ComputeTier::Architecture,
            });
            self.models.insert("google/gemini-3.8-flash".to_string(), ModelMetadata {
                id: "google/gemini-3.8-flash".to_string(),
                provider: "google".to_string(),
                created: 1779000000,
                context_length: 2000000,
                tools_supported: true,
                reasoning_supported: false,
                tier: ComputeTier::Implementation,
            });
            self.models.insert("deepseek/deepseek-v4.1-flash".to_string(), ModelMetadata {
                id: "deepseek/deepseek-v4.1-flash".to_string(),
                provider: "deepseek".to_string(),
                created: 1778000000,
                context_length: 128000,
                tools_supported: true,
                reasoning_supported: false,
                tier: ComputeTier::Implementation,
            });
        // Unconditionally add opencode-go first-class models
            self.models.insert("z-ai/glm-5.3-prime".to_string(), ModelMetadata {
                id: "z-ai/glm-5.3-prime".to_string(),
                provider: "z-ai".to_string(),
                created: 1777000000,
                context_length: 128000,
                tools_supported: true,
                reasoning_supported: true,
                tier: ComputeTier::Reasoning,
            });
        }
        
        // Unconditionally add opencode-go first-class models
        self.models.insert("opencode-go/glm-5.3".to_string(), ModelMetadata {
            id: "opencode-go/glm-5.3".to_string(),
            provider: "opencode-go".to_string(),
            created: 1781000000,
            context_length: 128000,
            tools_supported: true,
            reasoning_supported: true,
            tier: ComputeTier::Reasoning,
        });
        self.models.insert("opencode-go/glm-5.3-flash".to_string(), ModelMetadata {
            id: "opencode-go/glm-5.3-flash".to_string(),
            provider: "opencode-go".to_string(),
            created: 1781000000,
            context_length: 128000,
            tools_supported: true,
            reasoning_supported: false,
            tier: ComputeTier::Implementation,
        });
        self.models.insert("opencode-go/deepseek-v4-flash".to_string(), ModelMetadata {
            id: "opencode-go/deepseek-v4-flash".to_string(),
            provider: "opencode-go".to_string(),
            created: 1781000000,
            context_length: 128000,
            tools_supported: true,
            reasoning_supported: false,
            tier: ComputeTier::Implementation,
        });
        self.models.insert("opencode-go/kimi-k3".to_string(), ModelMetadata {
            id: "opencode-go/kimi-k3".to_string(),
            provider: "opencode-go".to_string(),
            created: 1781000000,
            context_length: 256000,
            tools_supported: true,
            reasoning_supported: true,
            tier: ComputeTier::Architecture,
        });
    }
}
