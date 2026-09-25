use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelMetadata {
    pub id: String,
    pub provider: String,
    pub created: u64, // Descending epoch timestamps for recency
    pub context_length: u32,
    pub tools_supported: bool,
    pub reasoning_supported: bool,
    pub tier: ComputeTier,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord, Copy, Default)]
#[serde(rename_all = "lowercase")]
pub enum ComputeTier {
    #[default]
    Base = 0,
    Implementation = 1,
    Reasoning = 2,
    Architecture = 3,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RoutingDecision {
    pub primary_model: String,
    pub primary_provider: String,
    pub fallback_model: Option<String>,
    pub fallback_provider: Option<String>,
    pub explanation: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequest {
    pub messages: Vec<serde_json::Value>,
    #[serde(default)]
    pub tools_required: bool,
    #[serde(default)]
    pub reasoning_required: bool,
    #[serde(default)]
    pub tier: ComputeTier,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}
