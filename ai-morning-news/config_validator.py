#!/usr/bin/env python3
"""
Config validator for config.json

Checks for required keys, valid structures, and common configuration errors.
Exports:
  - validate_config(config: dict) -> list[str]: Returns warnings/errors (empty = all good)
  - validate_and_warn(config: dict): Logs warnings via logger
"""

from logger import get_logger

log = get_logger('config_validator')

# Known valid keys for source objects
VALID_SOURCE_KEYS = {
    'name', 'url', 'icon', 'color', 'category',
    'description', 'ai_only', 'enabled', 'disabled', 'type', 'tier'
}

def validate_config(config: dict) -> list[str]:
    """
    Validates config.json structure.

    Returns:
        list[str]: Warning/error messages. Empty list = all good.
    """
    issues = []

    # 1. Check required top-level keys
    required_keys = {'llm', 'sources', 'settings'}
    missing = required_keys - set(config.keys())
    if missing:
        issues.append(f"Missing required top-level keys: {', '.join(sorted(missing))}")
        # If critical keys missing, can't proceed with deeper checks
        if 'sources' in missing or 'settings' in missing:
            return issues

    # 2. Check LLM config
    llm = config.get('llm', {})
    if llm.get('enabled', False):
        if not llm.get('base_url'):
            issues.append("llm.enabled=true but llm.base_url is missing or empty")
        elif not isinstance(llm.get('base_url'), str):
            issues.append("llm.base_url must be a string")

        if not llm.get('model'):
            issues.append("llm.enabled=true but llm.model is missing or empty")
        elif not isinstance(llm.get('model'), str):
            issues.append("llm.model must be a string")

    # 3. Check sources structure
    sources = config.get('sources', {})
    source_names = set()

    for lang in ('english', 'chinese'):
        lang_sources = sources.get(lang, [])
        if not isinstance(lang_sources, list):
            issues.append(f"sources.{lang} must be a list, got {type(lang_sources).__name__}")
            continue

        for idx, source in enumerate(lang_sources):
            if not isinstance(source, dict):
                issues.append(f"sources.{lang}[{idx}] must be a dict, got {type(source).__name__}")
                continue

            # Check required source keys
            src_name = source.get('name', f'<unnamed at {lang}[{idx}]>')
            source_names.add(src_name)

            if 'name' not in source:
                issues.append(f"sources.{lang}[{idx}]: missing 'name' key")
            elif not isinstance(source.get('name'), str):
                issues.append(f"sources.{lang}[{idx}].name must be string")

            if 'url' not in source:
                issues.append(f"sources.{lang}[{idx}] ({src_name}): missing 'url' key")
            elif not isinstance(source.get('url'), str):
                issues.append(f"sources.{lang}[{idx}] ({src_name}).url must be string")

            for required_key in ('icon', 'color', 'category'):
                if required_key not in source:
                    issues.append(f"sources.{lang}[{idx}] ({src_name}): missing '{required_key}' key")

            # Check tier value
            tier = source.get('tier')
            if tier is not None:
                if not isinstance(tier, int) or tier not in (0, 1, 2):
                    issues.append(f"sources.{lang}[{idx}] ({src_name}): tier must be 0, 1, or 2, got {tier}")
            else:
                issues.append(f"sources.{lang}[{idx}] ({src_name}): missing 'tier' key (will default to 2)")

            # Check for unknown keys (possible typos)
            unknown = set(source.keys()) - VALID_SOURCE_KEYS - {k for k in source.keys() if k.startswith('_')}
            if unknown:
                issues.append(f"sources.{lang}[{idx}] ({src_name}): unknown keys {sorted(unknown)} (possible typos?)")

    # 4. Check settings
    settings = config.get('settings', {})

    max_items = settings.get('max_items_per_source')
    if max_items is not None:
        if not isinstance(max_items, int):
            issues.append("settings.max_items_per_source must be integer")
        elif max_items <= 0:
            issues.append("settings.max_items_per_source must be positive")

    max_age = settings.get('max_age_hours')
    if max_age is not None:
        if not isinstance(max_age, int):
            issues.append("settings.max_age_hours must be integer")
        elif max_age <= 0:
            issues.append("settings.max_age_hours must be positive")

    output_file = settings.get('output_file')
    if output_file is not None:
        if not isinstance(output_file, str):
            issues.append("settings.output_file must be string")

    # 5. Check source_authority references
    source_authority = config.get('source_authority', {})
    for authority_name in source_authority.keys():
        if authority_name.startswith('_'):
            # Skip comment keys
            continue
        if authority_name not in source_names:
            issues.append(f"source_authority has '{authority_name}' but no matching source found (check spelling)")

    return issues


def validate_and_warn(config: dict):
    """
    Validates config and logs any issues as warnings.

    Args:
        config (dict): The loaded config dictionary
    """
    issues = validate_config(config)
    if issues:
        for issue in issues:
            log.warning("⚠️ Config issue: %s", issue)
    else:
        log.info("✓ Config validation passed")
