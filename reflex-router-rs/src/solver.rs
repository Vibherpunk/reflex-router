use crate::catalog::LiveCatalog;
use crate::models::{ComputeTier, RoutingDecision};

pub fn resolve_route(
    catalog: &LiveCatalog,
    tools_required: bool,
    reasoning_required: bool,
    tier: ComputeTier,
) -> RoutingDecision {
    let mut valid_models = Vec::new();

    for entry in catalog.models.iter() {
        let model = entry.value();

        if tools_required && !model.tools_supported {
            continue;
        }
        if reasoning_required && !model.reasoning_supported {
            continue;
        }
        if model.tier < tier {
            continue;
        }
        valid_models.push(model.clone());
    }

    // Sort by 'created' epoch integer timestamp descending for newest models
    valid_models.sort_by_key(|b| std::cmp::Reverse(b.created));

    if valid_models.is_empty() {
        return RoutingDecision {
            primary_model: "fallback-default".to_string(),
            primary_provider: "openai".to_string(),
            fallback_model: None,
            fallback_provider: None,
            explanation: "No models matched capabilities".to_string(),
        };
    }

    let primary = &valid_models[0];
    let fallback = valid_models.get(1);

    RoutingDecision {
        primary_model: primary.id.clone(),
        primary_provider: primary.provider.clone(),
        fallback_model: fallback.map(|m| m.id.clone()),
        fallback_provider: fallback.map(|m| m.provider.clone()),
        explanation: format!(
            "Resolved route for tier {:?}, newest models chosen",
            tier
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::ModelMetadata;

    #[test]
    fn test_resolve_route_capabilities() {
        let catalog = LiveCatalog::new();
        catalog.models.insert("old-base".to_string(), ModelMetadata {
            id: "old-base".to_string(),
            provider: "openai".to_string(),
            created: 1000,
            context_length: 4096,
            tools_supported: false,
            reasoning_supported: false,
            tier: ComputeTier::Base,
        });
        catalog.models.insert("new-base".to_string(), ModelMetadata {
            id: "new-base".to_string(),
            provider: "anthropic".to_string(),
            created: 2000,
            context_length: 8192,
            tools_supported: false,
            reasoning_supported: false,
            tier: ComputeTier::Base,
        });
        catalog.models.insert("tool-model".to_string(), ModelMetadata {
            id: "tool-model".to_string(),
            provider: "google".to_string(),
            created: 1500,
            context_length: 8192,
            tools_supported: true,
            reasoning_supported: false,
            tier: ComputeTier::Implementation,
        });
        catalog.models.insert("reasoning-model".to_string(), ModelMetadata {
            id: "reasoning-model".to_string(),
            provider: "openai".to_string(),
            created: 2500,
            context_length: 8192,
            tools_supported: true,
            reasoning_supported: true,
            tier: ComputeTier::Reasoning,
        });

        // Test timestamp-based latest model resolution for base
        let dec1 = resolve_route(&catalog, false, false, ComputeTier::Base);
        assert_eq!(dec1.primary_model, "reasoning-model"); // created 2500, highest timestamp
        
        // Test capability filtering (needs reasoning)
        let dec2 = resolve_route(&catalog, false, true, ComputeTier::Base);
        assert_eq!(dec2.primary_model, "reasoning-model");
        
        // Test capability filtering (needs tools, but not reasoning) -> highest created is reasoning-model
        let dec3 = resolve_route(&catalog, true, false, ComputeTier::Base);
        assert_eq!(dec3.primary_model, "reasoning-model");

        // Test tier filtering
        let dec4 = resolve_route(&catalog, false, false, ComputeTier::Architecture);
        assert_eq!(dec4.primary_model, "fallback-default"); // No architecture tier models
    }
}
