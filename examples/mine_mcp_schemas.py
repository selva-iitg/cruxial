"""Mine tool schemas from well-known public MCP servers.

Spawns each server via stdio (npx or uvx) and asks for ``tools/list``. The
resulting `{server_id: {tool_name: schema}}` is serialised to a Python
literal so it can be imported as a frozen corpus for offline benchmarking.

This script needs `npx` (Node.js) and/or `uvx` (uv-tool) installed locally.
Each server is downloaded on first run; subsequent runs are cached by npm/uv.

Run:
    python examples/mine_mcp_schemas.py
    # writes cruxial/demo/mcp_schemas.py

Add new servers by appending to MCP_SERVERS below. Format is:
    {
        "id": "filesystem",                              # short unique key
        "command": "npx",                                # spawn binary
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "domain": "storage",                             # for benchmark grouping
        "notes": "Anthropic reference server",
    }

Servers that fail to spawn are reported and skipped — the snapshot still
proceeds with whatever succeeded.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import tempfile
from pathlib import Path
from typing import Any

from cruxial.adapters.mcp import import_server_stdio


# ─── catalogue ───────────────────────────────────────────────────────
# Curated set of well-known public MCP servers. Sources:
#   - github.com/modelcontextprotocol/servers (Anthropic reference)
#   - github.com/punkpeye/awesome-mcp-servers (community list)
#   - Individual vendor repos linked from modelcontextprotocol.io


_TMP = tempfile.mkdtemp(prefix="cruxial-mcp-mine-")


MCP_SERVERS: list[dict[str, Any]] = [
    # ── Anthropic reference servers (npm) ─────────────────────────────
    {
        "id": "filesystem",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", _TMP],
        "domain": "storage", "notes": "Anthropic reference — local FS access",
    },
    {
        "id": "memory",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"],
        "domain": "memory", "notes": "Anthropic reference — knowledge graph store",
    },
    {
        "id": "sequential-thinking",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "domain": "cognition", "notes": "Anthropic reference — chained reasoning",
    },
    {
        "id": "everything",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"],
        "domain": "test-bench", "notes": "Anthropic reference — exercises every MCP feature",
    },
    {
        "id": "fetch",
        "command": "uvx", "args": ["mcp-server-fetch"],
        "domain": "web", "notes": "Anthropic reference — URL fetcher",
    },
    {
        "id": "git",
        "command": "uvx", "args": ["mcp-server-git", "--repository", _TMP],
        "domain": "devops", "notes": "Anthropic reference — git operations",
    },
    {
        "id": "time",
        "command": "uvx", "args": ["mcp-server-time"],
        "domain": "utility", "notes": "Anthropic reference — time/timezone",
    },
    {
        "id": "sqlite",
        "command": "uvx", "args": ["mcp-server-sqlite", "--db-path", f"{_TMP}/test.db"],
        "domain": "data", "notes": "Anthropic reference — SQLite query",
    },

    # ── Community servers (high-traffic) ──────────────────────────────
    {
        "id": "playwright",
        "command": "npx", "args": ["-y", "@playwright/mcp@latest"],
        "domain": "browser-automation", "notes": "Microsoft — browser automation",
    },
    {
        "id": "puppeteer",
        "command": "npx", "args": ["-y", "@hisma/server-puppeteer"],
        "domain": "browser-automation", "notes": "Community fork — Puppeteer",
    },
    {
        "id": "brave-search",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "domain": "web-search", "notes": "Brave Search API (needs BRAVE_API_KEY)",
        "env": {"BRAVE_API_KEY": "demo"},
    },
    {
        "id": "duckduckgo",
        "command": "uvx", "args": ["duckduckgo-mcp-server"],
        "domain": "web-search", "notes": "DuckDuckGo search",
    },
    {
        "id": "youtube-transcript",
        "command": "npx", "args": ["-y", "@kimtaeyoon83/mcp-server-youtube-transcript"],
        "domain": "media", "notes": "YouTube transcript fetcher",
    },
    {
        "id": "github",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
        "domain": "devops", "notes": "GitHub API (needs GITHUB_PERSONAL_ACCESS_TOKEN)",
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "gitlab",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-gitlab"],
        "domain": "devops", "notes": "GitLab API",
        "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": "demo", "GITLAB_API_URL": "https://gitlab.com/api/v4"},
    },
    {
        "id": "slack",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-slack"],
        "domain": "messaging", "notes": "Slack workspace operations",
        "env": {"SLACK_BOT_TOKEN": "demo", "SLACK_TEAM_ID": "demo"},
    },
    {
        "id": "google-drive",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-gdrive"],
        "domain": "storage", "notes": "Google Drive",
        "env": {"GDRIVE_CREDENTIALS_PATH": "/tmp/nonexistent"},
    },
    {
        "id": "postgres",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://demo:demo@localhost/demo"],
        "domain": "data", "notes": "Postgres read-only query",
    },
    {
        "id": "redis",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-redis", "redis://localhost:6379"],
        "domain": "data", "notes": "Redis operations",
    },
    {
        "id": "sentry",
        "command": "uvx", "args": ["mcp-server-sentry", "--auth-token", "demo"],
        "domain": "observability", "notes": "Sentry issues / events",
    },
    {
        "id": "linear",
        "command": "npx", "args": ["-y", "@jerhadf/linear-mcp-server"],
        "domain": "project-mgmt", "notes": "Linear API",
        "env": {"LINEAR_API_KEY": "demo"},
    },
    {
        "id": "notion",
        "command": "npx", "args": ["-y", "@suekou/mcp-notion-server"],
        "domain": "knowledge", "notes": "Notion pages + databases",
        "env": {"NOTION_API_TOKEN": "demo"},
    },
    {
        "id": "obsidian",
        "command": "npx", "args": ["-y", "@cyanheads/obsidian-mcp-server"],
        "domain": "knowledge", "notes": "Obsidian vault",
        "env": {"OBSIDIAN_API_KEY": "demo"},
    },
    {
        "id": "airtable",
        "command": "npx", "args": ["-y", "airtable-mcp-server"],
        "domain": "data", "notes": "Airtable bases",
        "env": {"AIRTABLE_API_KEY": "demo"},
    },
    {
        "id": "stripe",
        "command": "npx", "args": ["-y", "@stripe/mcp", "--tools=all", "--api-key=sk_test_demo"],
        "domain": "payments", "notes": "Stripe API",
    },
    {
        "id": "cloudflare",
        "command": "npx", "args": ["-y", "@cloudflare/mcp-server-cloudflare", "init"],
        "domain": "infrastructure", "notes": "Cloudflare workers / KV / etc.",
        "env": {"CLOUDFLARE_API_TOKEN": "demo"},
    },
    {
        "id": "kubernetes",
        "command": "npx", "args": ["-y", "mcp-server-kubernetes"],
        "domain": "infrastructure", "notes": "K8s read operations",
    },
    {
        "id": "docker",
        "command": "uvx", "args": ["docker-mcp"],
        "domain": "infrastructure", "notes": "Docker daemon",
    },
    {
        "id": "elastic",
        "command": "npx", "args": ["-y", "@elastic/mcp-server-elasticsearch"],
        "domain": "data", "notes": "Elasticsearch queries",
        "env": {"ES_URL": "http://localhost:9200"},
    },
    {
        "id": "browserbase",
        "command": "npx", "args": ["-y", "@browserbasehq/mcp-server-browserbase"],
        "domain": "browser-automation", "notes": "Browserbase cloud browser",
        "env": {"BROWSERBASE_API_KEY": "demo", "BROWSERBASE_PROJECT_ID": "demo"},
    },
    {
        "id": "perplexity",
        "command": "npx", "args": ["-y", "server-perplexity-ask"],
        "domain": "web-search", "notes": "Perplexity search",
        "env": {"PERPLEXITY_API_KEY": "demo"},
    },
    {
        "id": "exa",
        "command": "npx", "args": ["-y", "exa-mcp-server"],
        "domain": "web-search", "notes": "Exa semantic search",
        "env": {"EXA_API_KEY": "demo"},
    },
    {
        "id": "firecrawl",
        "command": "npx", "args": ["-y", "firecrawl-mcp"],
        "domain": "web", "notes": "Firecrawl scraping",
        "env": {"FIRECRAWL_API_KEY": "demo"},
    },
    {
        "id": "tavily",
        "command": "npx", "args": ["-y", "tavily-mcp"],
        "domain": "web-search", "notes": "Tavily AI-native search",
        "env": {"TAVILY_API_KEY": "demo"},
    },
    {
        "id": "sequentialthinking-tools",
        "command": "npx", "args": ["-y", "mcp-sequentialthinking-tools"],
        "domain": "cognition", "notes": "Sequential thinking + tool routing",
    },

    # ─── EXPANDED CATALOGUE for V0.1 launch benchmark ─────────────────
    # Covers most-used / complex / domain-specific / vendor-official /
    # rapid-update / biggest-schema servers.

    # ── Productivity / project mgmt ──────────────────────────────────
    {
        "id": "asana",
        "command": "npx", "args": ["-y", "@roychri/mcp-server-asana"],
        "domain": "productivity", "notes": "Asana tasks + projects",
        "env": {"ASANA_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "todoist",
        "command": "npx", "args": ["-y", "@abhiz123/todoist-mcp-server"],
        "domain": "productivity", "notes": "Todoist tasks",
        "env": {"TODOIST_API_TOKEN": "demo"},
    },
    {
        "id": "trello",
        "command": "npx", "args": ["-y", "@delorenj/mcp-server-trello"],
        "domain": "productivity", "notes": "Trello boards + cards",
        "env": {"TRELLO_API_KEY": "demo", "TRELLO_TOKEN": "demo", "TRELLO_BOARD_ID": "demo"},
    },
    {
        "id": "clickup",
        "command": "npx", "args": ["-y", "@hauptsache.net/clickup-mcp"],
        "domain": "productivity", "notes": "ClickUp tasks + workspaces",
        "env": {"CLICKUP_API_KEY": "demo"},
    },

    # ── Payments / Finance ───────────────────────────────────────────
    {
        "id": "stripe-agent-toolkit",
        "command": "npx", "args": ["-y", "@stripe/agent-toolkit", "--tools=all"],
        "domain": "payments", "notes": "Stripe — broader toolkit fork",
        "env": {"STRIPE_SECRET_KEY": "sk_test_demo"},
    },
    {
        "id": "paypal",
        "command": "npx", "args": ["-y", "@paypal/mcp"],
        "domain": "payments", "notes": "PayPal merchant operations",
        "env": {"PAYPAL_ACCESS_TOKEN": "demo", "PAYPAL_ENVIRONMENT": "SANDBOX"},
    },
    {
        "id": "quickbooks",
        "command": "npx", "args": ["-y", "@dvncan/quickbooks-mcp"],
        "domain": "finance", "notes": "QuickBooks bookkeeping",
        "env": {"QUICKBOOKS_ACCESS_TOKEN": "demo", "QUICKBOOKS_REALM_ID": "demo"},
    },
    {
        "id": "plaid",
        "command": "npx", "args": ["-y", "@plaid/mcp-server"],
        "domain": "finance", "notes": "Plaid bank connections",
        "env": {"PLAID_CLIENT_ID": "demo", "PLAID_SECRET": "demo"},
    },

    # ── Marketing / email / outbound ─────────────────────────────────
    {
        "id": "mailchimp",
        "command": "npx", "args": ["-y", "mailchimp-mcp"],
        "domain": "marketing", "notes": "Mailchimp campaigns",
        "env": {"MAILCHIMP_API_KEY": "demo"},
    },
    {
        "id": "resend",
        "command": "npx", "args": ["-y", "mcp-send-email"],
        "domain": "marketing", "notes": "Transactional email via Resend",
        "env": {"RESEND_API_KEY": "demo", "SENDER_EMAIL_ADDRESS": "demo@example.com"},
    },
    {
        "id": "sendgrid",
        "command": "npx", "args": ["-y", "@sendgrid/mcp-server"],
        "domain": "marketing", "notes": "SendGrid transactional + marketing",
        "env": {"SENDGRID_API_KEY": "demo"},
    },
    {
        "id": "hubspot",
        "command": "npx", "args": ["-y", "@hubspot/mcp-server"],
        "domain": "crm-marketing", "notes": "HubSpot CRM + marketing",
        "env": {"PRIVATE_APP_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "klaviyo",
        "command": "npx", "args": ["-y", "@klaviyo/mcp"],
        "domain": "marketing", "notes": "Klaviyo audience + flows",
        "env": {"KLAVIYO_API_KEY": "demo"},
    },

    # ── CRM / Sales ──────────────────────────────────────────────────
    {
        "id": "salesforce",
        "command": "npx", "args": ["-y", "@tsmztech/mcp-server-salesforce"],
        "domain": "crm", "notes": "Salesforce SOQL + CRUD",
        "env": {"SALESFORCE_USERNAME": "demo", "SALESFORCE_PASSWORD": "demo", "SALESFORCE_TOKEN": "demo"},
    },
    {
        "id": "attio",
        "command": "npx", "args": ["-y", "@attio/mcp-server"],
        "domain": "crm", "notes": "Attio CRM",
        "env": {"ATTIO_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "intercom",
        "command": "npx", "args": ["-y", "@intercom/mcp"],
        "domain": "customer-support", "notes": "Intercom conversations",
        "env": {"INTERCOM_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "zendesk",
        "command": "npx", "args": ["-y", "zendesk-mcp"],
        "domain": "customer-support", "notes": "Zendesk tickets",
        "env": {"ZENDESK_SUBDOMAIN": "demo", "ZENDESK_EMAIL": "demo@example.com", "ZENDESK_API_TOKEN": "demo"},
    },

    # ── Atlassian ────────────────────────────────────────────────────
    {
        "id": "atlassian",
        "command": "npx", "args": ["-y", "@aashari/mcp-server-atlassian-jira"],
        "domain": "productivity", "notes": "Jira issues + projects",
        "env": {"ATLASSIAN_SITE_NAME": "demo", "ATLASSIAN_USER_EMAIL": "demo@example.com", "ATLASSIAN_API_TOKEN": "demo"},
    },
    {
        "id": "confluence",
        "command": "npx", "args": ["-y", "@aashari/mcp-server-atlassian-confluence"],
        "domain": "knowledge", "notes": "Confluence pages",
        "env": {"ATLASSIAN_SITE_NAME": "demo", "ATLASSIAN_USER_EMAIL": "demo@example.com", "ATLASSIAN_API_TOKEN": "demo"},
    },

    # ── Observability / monitoring ───────────────────────────────────
    {
        "id": "datadog",
        "command": "npx", "args": ["-y", "@datadog/mcp"],
        "domain": "observability", "notes": "Datadog metrics + logs",
        "env": {"DD_API_KEY": "demo", "DD_APP_KEY": "demo"},
    },
    {
        "id": "pagerduty",
        "command": "npx", "args": ["-y", "@pagerduty/mcp-server"],
        "domain": "observability", "notes": "PagerDuty incidents + on-call",
        "env": {"PAGERDUTY_API_TOKEN": "demo"},
    },
    {
        "id": "honeycomb",
        "command": "npx", "args": ["-y", "@honeycombio/honeycomb-mcp"],
        "domain": "observability", "notes": "Honeycomb traces + queries",
        "env": {"HONEYCOMB_API_KEY": "demo"},
    },
    {
        "id": "grafana",
        "command": "npx", "args": ["-y", "@grafana/mcp-grafana"],
        "domain": "observability", "notes": "Grafana dashboards + alerts",
        "env": {"GRAFANA_URL": "http://localhost:3000", "GRAFANA_API_KEY": "demo"},
    },

    # ── Cloud infra ──────────────────────────────────────────────────
    {
        "id": "aws",
        "command": "uvx", "args": ["awslabs.aws-documentation-mcp-server@latest"],
        "domain": "infrastructure", "notes": "AWS docs MCP (no AWS auth needed)",
    },
    {
        "id": "vercel",
        "command": "npx", "args": ["-y", "@vercel/mcp-adapter"],
        "domain": "infrastructure", "notes": "Vercel deployments",
        "env": {"VERCEL_TOKEN": "demo"},
    },
    {
        "id": "render",
        "command": "npx", "args": ["-y", "@niyogi/render-mcp"],
        "domain": "infrastructure", "notes": "Render services",
        "env": {"RENDER_API_KEY": "demo"},
    },
    {
        "id": "supabase",
        "command": "npx", "args": ["-y", "@supabase/mcp-server-supabase@latest"],
        "domain": "data", "notes": "Supabase project ops",
        "env": {"SUPABASE_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "planetscale",
        "command": "npx", "args": ["-y", "@planetscale/mcp-server-planetscale"],
        "domain": "data", "notes": "PlanetScale serverless mysql",
        "env": {"PLANETSCALE_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "fly-io",
        "command": "npx", "args": ["-y", "fly-mcp"],
        "domain": "infrastructure", "notes": "Fly.io apps + machines",
        "env": {"FLY_API_TOKEN": "demo"},
    },

    # ── Data / analytics ─────────────────────────────────────────────
    {
        "id": "snowflake",
        "command": "uvx", "args": ["mcp-server-snowflake"],
        "domain": "data", "notes": "Snowflake SQL queries",
        "env": {"SNOWFLAKE_ACCOUNT": "demo", "SNOWFLAKE_USER": "demo", "SNOWFLAKE_PASSWORD": "demo", "SNOWFLAKE_WAREHOUSE": "demo"},
    },
    {
        "id": "clickhouse",
        "command": "uvx", "args": ["mcp-clickhouse"],
        "domain": "data", "notes": "ClickHouse analytics",
        "env": {"CLICKHOUSE_HOST": "localhost", "CLICKHOUSE_USER": "default", "CLICKHOUSE_PASSWORD": ""},
    },
    {
        "id": "bigquery",
        "command": "uvx", "args": ["mcp-server-bigquery", "--project", "demo"],
        "domain": "data", "notes": "Google BigQuery",
    },
    {
        "id": "pinecone",
        "command": "npx", "args": ["-y", "@pinecone-database/mcp"],
        "domain": "vector-db", "notes": "Pinecone vector search",
        "env": {"PINECONE_API_KEY": "demo"},
    },
    {
        "id": "qdrant",
        "command": "uvx", "args": ["mcp-server-qdrant"],
        "domain": "vector-db", "notes": "Qdrant vector DB",
        "env": {"QDRANT_URL": "http://localhost:6333", "COLLECTION_NAME": "demo"},
    },
    {
        "id": "neo4j",
        "command": "uvx", "args": ["mcp-neo4j-cypher"],
        "domain": "data", "notes": "Neo4j graph queries",
        "env": {"NEO4J_URI": "bolt://localhost:7687", "NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": "demo"},
    },
    {
        "id": "mongodb",
        "command": "npx", "args": ["-y", "mongodb-mcp-server"],
        "domain": "data", "notes": "MongoDB operations",
        "env": {"MDB_MCP_CONNECTION_STRING": "mongodb://demo:demo@localhost:27017"},
    },

    # ── Communication ────────────────────────────────────────────────
    {
        "id": "discord",
        "command": "npx", "args": ["-y", "@v-3/discordmcp"],
        "domain": "messaging", "notes": "Discord bot operations",
        "env": {"DISCORD_TOKEN": "demo"},
    },
    {
        "id": "telegram",
        "command": "npx", "args": ["-y", "telegram-mcp"],
        "domain": "messaging", "notes": "Telegram bot ops",
        "env": {"TELEGRAM_BOT_TOKEN": "demo"},
    },
    {
        "id": "twilio",
        "command": "npx", "args": ["-y", "@twilio-alpha/mcp"],
        "domain": "messaging", "notes": "Twilio SMS + voice",
        "env": {"TWILIO_ACCOUNT_SID": "AC_demo", "TWILIO_API_KEY": "SK_demo", "TWILIO_API_SECRET": "demo"},
    },
    {
        "id": "ms-teams",
        "command": "npx", "args": ["-y", "@floriscornel/teams-mcp"],
        "domain": "messaging", "notes": "Microsoft Teams",
        "env": {"TEAMS_TENANT_ID": "demo", "TEAMS_CLIENT_ID": "demo", "TEAMS_CLIENT_SECRET": "demo"},
    },

    # ── Productivity apps (Google / Microsoft / Apple) ───────────────
    {
        "id": "google-calendar",
        "command": "npx", "args": ["-y", "@cocal/google-calendar-mcp"],
        "domain": "productivity", "notes": "Google Calendar",
        "env": {"GOOGLE_OAUTH_CREDENTIALS": "/tmp/nonexistent"},
    },
    {
        "id": "google-sheets",
        "command": "npx", "args": ["-y", "google-sheets-mcp"],
        "domain": "productivity", "notes": "Google Sheets",
        "env": {"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/nonexistent"},
    },
    {
        "id": "outlook",
        "command": "npx", "args": ["-y", "outlook-mcp"],
        "domain": "productivity", "notes": "Outlook email + calendar",
        "env": {"MS_GRAPH_TOKEN": "demo"},
    },
    {
        "id": "apple-calendar",
        "command": "npx", "args": ["-y", "apple-calendar-mcp"],
        "domain": "productivity", "notes": "Apple Calendar (macOS only)",
    },

    # ── Healthcare / clinical ────────────────────────────────────────
    {
        "id": "fhir",
        "command": "uvx", "args": ["mcp-server-fhir"],
        "domain": "healthcare", "notes": "HL7 FHIR servers (read-only ops)",
        "env": {"FHIR_BASE_URL": "https://hapi.fhir.org/baseR4"},
    },
    {
        "id": "pubmed",
        "command": "uvx", "args": ["pubmedmcp"],
        "domain": "healthcare-research", "notes": "PubMed medical literature search",
    },

    # ── E-commerce ───────────────────────────────────────────────────
    {
        "id": "shopify",
        "command": "npx", "args": ["-y", "@shopify/mcp-server-shopify"],
        "domain": "ecommerce", "notes": "Shopify storefront + admin",
        "env": {"SHOPIFY_ACCESS_TOKEN": "demo", "SHOPIFY_SHOP": "demo"},
    },
    {
        "id": "amazon-s3",
        "command": "npx", "args": ["-y", "s3-mcp"],
        "domain": "storage", "notes": "AWS S3 buckets",
        "env": {"AWS_ACCESS_KEY_ID": "demo", "AWS_SECRET_ACCESS_KEY": "demo"},
    },

    # ── Browser automation extras ────────────────────────────────────
    {
        "id": "selenium",
        "command": "uvx", "args": ["mcp-selenium"],
        "domain": "browser-automation", "notes": "Selenium WebDriver",
    },

    # ── Calendaring extras ───────────────────────────────────────────
    {
        "id": "cal-com",
        "command": "npx", "args": ["-y", "@cal/mcp"],
        "domain": "productivity", "notes": "Cal.com scheduling",
        "env": {"CAL_API_KEY": "demo"},
    },

    # ── Design / collaboration ───────────────────────────────────────
    {
        "id": "figma",
        "command": "npx", "args": ["-y", "figma-mcp"],
        "domain": "design", "notes": "Figma files + comments",
        "env": {"FIGMA_ACCESS_TOKEN": "demo"},
    },
    {
        "id": "miro",
        "command": "npx", "args": ["-y", "miro-mcp"],
        "domain": "design", "notes": "Miro boards",
        "env": {"MIRO_ACCESS_TOKEN": "demo"},
    },

    # ── Video / meetings ─────────────────────────────────────────────
    {
        "id": "zoom",
        "command": "npx", "args": ["-y", "@zoom/mcp"],
        "domain": "video", "notes": "Zoom meetings + recordings",
        "env": {"ZOOM_OAUTH_TOKEN": "demo"},
    },
    {
        "id": "loom",
        "command": "npx", "args": ["-y", "loom-mcp"],
        "domain": "video", "notes": "Loom recordings",
        "env": {"LOOM_API_KEY": "demo"},
    },

    # ── AI / RAG infra ───────────────────────────────────────────────
    {
        "id": "anthropic",
        "command": "npx", "args": ["-y", "@anthropic/mcp"],
        "domain": "ai", "notes": "Anthropic API operations",
        "env": {"ANTHROPIC_API_KEY": "demo"},
    },
    {
        "id": "openai-tools",
        "command": "npx", "args": ["-y", "openai-mcp-server"],
        "domain": "ai", "notes": "OpenAI API ops",
        "env": {"OPENAI_API_KEY": "demo"},
    },
    {
        "id": "weaviate",
        "command": "uvx", "args": ["mcp-server-weaviate"],
        "domain": "vector-db", "notes": "Weaviate vector DB",
        "env": {"WEAVIATE_URL": "http://localhost:8080"},
    },

    # ── Misc utilities + popular community servers ────────────────────
    {
        "id": "wolfram-alpha",
        "command": "npx", "args": ["-y", "wolfram-alpha-mcp"],
        "domain": "knowledge", "notes": "Wolfram Alpha computational",
        "env": {"WOLFRAM_APP_ID": "demo"},
    },
    {
        "id": "wikipedia",
        "command": "uvx", "args": ["mcp-server-wikipedia"],
        "domain": "knowledge", "notes": "Wikipedia search + read",
    },
    {
        "id": "arxiv",
        "command": "uvx", "args": ["arxiv-mcp-server"],
        "domain": "research", "notes": "arXiv paper search",
    },
    {
        "id": "spotify",
        "command": "npx", "args": ["-y", "@vansky/spotify-mcp"],
        "domain": "media", "notes": "Spotify playlists + playback",
        "env": {"SPOTIFY_CLIENT_ID": "demo", "SPOTIFY_CLIENT_SECRET": "demo"},
    },
    {
        "id": "youtube",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-youtube"],
        "domain": "media", "notes": "YouTube data API",
        "env": {"YOUTUBE_API_KEY": "demo"},
    },
    {
        "id": "context7",
        "command": "npx", "args": ["-y", "@upstash/context7-mcp"],
        "domain": "knowledge", "notes": "Context7 — library docs lookup",
    },

    # ── DevOps extras ────────────────────────────────────────────────
    {
        "id": "circleci",
        "command": "npx", "args": ["-y", "@circleci/mcp-server-circleci"],
        "domain": "devops", "notes": "CircleCI pipelines",
        "env": {"CIRCLECI_TOKEN": "demo"},
    },
    {
        "id": "buildkite",
        "command": "npx", "args": ["-y", "@buildkite/buildkite-mcp-server"],
        "domain": "devops", "notes": "Buildkite pipelines",
        "env": {"BUILDKITE_API_TOKEN": "demo"},
    },
    {
        "id": "terraform",
        "command": "uvx", "args": ["terraform-mcp-server"],
        "domain": "devops", "notes": "Terraform Registry ops",
    },

    # ── Knowledge base / search extras ───────────────────────────────
    {
        "id": "serpapi",
        "command": "npx", "args": ["-y", "@serpapi/mcp-server"],
        "domain": "web-search", "notes": "SerpAPI Google results",
        "env": {"SERPAPI_API_KEY": "demo"},
    },
    {
        "id": "kagi",
        "command": "uvx", "args": ["mcp-kagi"],
        "domain": "web-search", "notes": "Kagi search",
        "env": {"KAGI_API_KEY": "demo"},
    },

    # ── Identity / auth ──────────────────────────────────────────────
    {
        "id": "auth0",
        "command": "npx", "args": ["-y", "@auth0/auth0-mcp-server"],
        "domain": "identity", "notes": "Auth0 user + tenant ops",
        "env": {"AUTH0_TOKEN": "demo", "AUTH0_DOMAIN": "demo.auth0.com"},
    },

    # ── Customer feedback / surveys ──────────────────────────────────
    {
        "id": "posthog",
        "command": "npx", "args": ["-y", "@posthog/mcp"],
        "domain": "analytics", "notes": "PostHog product analytics",
        "env": {"POSTHOG_API_KEY": "demo"},
    },
    {
        "id": "amplitude",
        "command": "npx", "args": ["-y", "amplitude-mcp"],
        "domain": "analytics", "notes": "Amplitude events + funnels",
        "env": {"AMPLITUDE_API_KEY": "demo"},
    },
    {
        "id": "mixpanel",
        "command": "npx", "args": ["-y", "mixpanel-mcp"],
        "domain": "analytics", "notes": "Mixpanel events",
        "env": {"MIXPANEL_PROJECT_TOKEN": "demo"},
    },
]


# ─── runner ──────────────────────────────────────────────────────────


async def discover_one(spec: dict[str, Any]) -> tuple[str, dict | None, str | None]:
    """Returns (server_id, schemas_or_None, error_or_None)."""
    sid = spec["id"]
    try:
        schemas = await import_server_stdio(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
            timeout_seconds=spec.get("timeout", 25.0),
        )
        return (sid, schemas, None)
    except Exception as e:
        return (sid, None, f"{type(e).__name__}: {e}")


async def discover_all(specs: list[dict]) -> dict[str, dict]:
    """Discover sequentially — npx parallelism risks process explosion."""
    results: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []

    print(f"\nMining {len(specs)} MCP servers — this may take several minutes.\n")
    for i, spec in enumerate(specs, 1):
        sid = spec["id"]
        t0 = time.perf_counter()
        print(f"[{i:2d}/{len(specs)}] {sid:<30}", end=" ", flush=True)
        result = await discover_one(spec)
        elapsed = time.perf_counter() - t0
        _, schemas, err = result
        if schemas:
            print(f"✓ {len(schemas):>3} tool(s)  ({elapsed:5.1f}s)")
            results[sid] = {
                "domain": spec.get("domain", "uncategorized"),
                "notes": spec.get("notes", ""),
                "command": spec["command"],
                "args": spec.get("args", []),
                "schemas": schemas,
            }
        else:
            print(f"✗ {err[:80]}  ({elapsed:5.1f}s)")
            failures.append((sid, err))

    print(f"\nDiscovery complete: {len(results)} succeeded, {len(failures)} failed")
    if failures:
        print("\nFailures (common reasons: missing env vars, server requires auth, package not published):")
        for sid, err in failures:
            print(f"  {sid:<30}  {err[:120]}")
    return results


def emit_module(results: dict[str, dict], out_path: Path) -> None:
    """Render results as a Python module for offline use.

    Loads from a JSON literal at import time. JSON keeps the file diffable
    (no Python-specific syntax) while staying offline-safe.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_tools = sum(len(r["schemas"]) for r in results.values())
    domains = sorted({r["domain"] for r in results.values()})

    header = f'''"""Frozen snapshot of MCP server tool schemas, mined from public servers.

AUTO-GENERATED by examples/mine_mcp_schemas.py — do not hand-edit.
Re-run that script to refresh.

Stats:
  - {len(results)} servers across {len(domains)} domains
  - {total_tools} total tools / schemas
  - Domains: {", ".join(domains)}

Use:
    from cruxial.demo.mcp_schemas import MCP_SCHEMAS
    # MCP_SCHEMAS["filesystem"]["schemas"]["read_file"] -> JSON Schema dict

For a flat {{name: schema}} suitable for guard():
    from cruxial.demo.mcp_schemas import flat_schemas
    schemas = flat_schemas()  # all tools across all servers, name-prefixed by server
"""

from __future__ import annotations

import json
from typing import Any

# {len(results)} MCP servers · {total_tools} tools total
# The data is stored as a JSON literal — Python's True/False/None differ
# from JSON's true/false/null, so we json.loads() at import to get clean
# native types (None, True, False) throughout.
_RAW_JSON = r"""'''

    body = json.dumps(results, indent=2, sort_keys=True, default=str)

    json_close = '"""\n\nMCP_SCHEMAS: dict[str, dict[str, Any]] = json.loads(_RAW_JSON)\n'

    footer = '''


def flat_schemas(prefix_with_server: bool = True) -> dict[str, dict]:
    """Flatten MCP_SCHEMAS into a single ``{name: schema}`` dict.

    Args:
        prefix_with_server: If True, prefix tool names with their server id
                           (e.g. ``filesystem__read_file``) to avoid collisions.
                           If False, last-tool-wins on collision.
    """
    out: dict[str, dict] = {}
    for server_id, payload in MCP_SCHEMAS.items():
        for tool_name, schema in payload["schemas"].items():
            key = f"{server_id}__{tool_name}" if prefix_with_server else tool_name
            out[key] = schema
    return out


def by_domain() -> dict[str, list[str]]:
    """Return ``{domain: [server_id, ...]}`` for grouped benchmarking."""
    grouped: dict[str, list[str]] = {}
    for sid, payload in MCP_SCHEMAS.items():
        grouped.setdefault(payload["domain"], []).append(sid)
    return grouped
'''
    out_path.write_text(header + body + json_close + footer, encoding="utf-8")


def main() -> int:
    out_path = Path(__file__).parent.parent / "cruxial" / "demo" / "mcp_schemas.py"
    results = asyncio.run(discover_all(MCP_SERVERS))
    if not results:
        print("\nNo servers discovered — output not written.", file=sys.stderr)
        return 1
    emit_module(results, out_path)
    print(f"\nWrote {out_path} ({len(results)} servers, {sum(len(r['schemas']) for r in results.values())} tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
