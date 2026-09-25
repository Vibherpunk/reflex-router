use serde::Deserialize;
use std::fs;
use std::path::Path;

#[derive(Debug, Deserialize, Clone)]
pub struct ScoringSpec {
    pub scoring: Scoring,
    pub arbitration: Arbitration,
    pub classification: Classification,
    pub domain_specializations: std::collections::HashMap<String, DomainSpecialization>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Scoring {
    pub clamp: Clamp,
    pub default: DefaultScores,
    pub families: std::collections::HashMap<String, Family>,
    pub tier_deltas: std::collections::HashMap<String, TierDelta>,
    pub specialization_bonuses: std::collections::HashMap<String, SpecializationBonus>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Clamp {
    pub min: f64,
    pub max: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct DefaultScores {
    pub reasoning_capability: f64,
    pub architecture_score: f64,
    pub coding_score: f64,
    pub speed_score: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Family {
    pub baseline: Baseline,
    pub anchor_generation: f64,
    pub growth_rate: f64,
    pub max_bonus: f64,
    pub min_delta: f64,
    pub legacy_generations: Option<LegacyGenerations>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Baseline {
    pub reasoning: f64,
    pub architecture: f64,
    pub coding: f64,
    pub speed: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct LegacyGenerations {
    pub min: f64,
    pub max: f64,
    pub penalty: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct TierDelta {
    pub reasoning: f64,
    pub architecture: f64,
    pub coding: f64,
    pub speed: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct SpecializationBonus {
    pub reasoning_boost: Option<f64>,
    pub architecture_boost: Option<f64>,
    pub coding_boost: Option<f64>,
    pub max_speed: Option<f64>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Arbitration {
    pub fitness_override_threshold: f64,
    pub domain_override_threshold: f64,
    pub cost_penalty_divisor: f64,
    pub tier_fitness_weights: std::collections::HashMap<String, std::collections::HashMap<String, f64>>,
    pub tier_threshold_profiles: std::collections::HashMap<String, std::collections::HashMap<String, f64>>,
    pub memory_escalation_defaults: std::collections::HashMap<String, std::collections::HashMap<String, f64>>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Classification {
    pub memory_threshold: f64,
    pub word_count_tier1_threshold: u64,
    pub history_check_turns: u32,
}

#[derive(Debug, Deserialize, Clone)]
pub struct DomainSpecialization {
    pub boost: f64,
    pub keywords: Vec<String>,
    pub model_tags: Vec<String>,
}

pub fn load_config<P: AsRef<Path>>(path: P) -> Result<ScoringSpec, Box<dyn std::error::Error>> {
    let content = fs::read_to_string(path)?;
    let spec: ScoringSpec = serde_yaml::from_str(&content)?;
    Ok(spec)
}
