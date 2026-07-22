"""Live LLM benchmark against MCP server schemas.

Same shape as ``azure_demo_suite.py``, but uses a curated subset of the
mined MCP corpus. Produces the launch headline: "Cruxial intercepts X% of
LLM tool calls on N real public MCP servers."

This is the most credible benchmark we can run: real schemas from real
production servers, no synthetic adversarial design, real LLM picking tools
from natural-language prompts.

Why a curated subset and not the full corpus?
  - Cost: each prompt × LLM round-trip ≈ 1-2s on gpt-4o.
  - Some servers expose 40+ tools; the LLM gets overwhelmed and refuses to
    pick one. Most production agents use 3-15 tools at a time.
  - We want a benchmark, not a stress test.

Default subset: 8 servers covering 8 distinct domains, ~30 tools total.
Override via ``CRUXIAL_MCP_SERVERS=server1,server2`` env var.

Run:
    export AZURE_OPENAI_DEPLOYMENT=gpt-4o
    rm ~/.cruxial/telemetry.sqlite
    python examples/azure_mcp_suite.py
    cruxial stats --since 30m
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

from cruxial import GuardConfig, guard
from cruxial.adapters.openai import auto_repair_batch


# Comprehensive launch subset — 51 servers across all mined domains.
# Excludes only mailchimp (274 tools — too big for a single chat completion's
# tools array; would blow gpt-4o's effective tool-selection budget).
DEFAULT_SUBSET = [
    # devops / infra
    "github", "gitlab", "circleci", "kubernetes", "docker", "aws",
    # productivity / collaboration
    "trello", "asana", "todoist", "outlook", "atlassian", "confluence",
    "notion", "figma",
    # data + databases + vector
    "supabase", "airtable", "sqlite", "postgres", "redis", "clickhouse",
    "neo4j", "pinecone", "qdrant",
    # crm / sales / customer
    "hubspot", "zendesk", "salesforce",
    # web / browser
    "playwright", "puppeteer", "browserbase", "firecrawl", "fetch",
    # messaging
    "slack", "ms-teams",
    # search / knowledge
    "brave-search", "duckduckgo", "tavily", "exa", "perplexity", "context7",
    "arxiv", "pubmed",
    # AI / cognition
    "openai-tools", "sequential-thinking", "sequentialthinking-tools",
    # media / file system
    "filesystem", "loom", "youtube-transcript",
    # observability
    "sentry",
    # utility / misc
    "time", "memory", "everything",
]

# Simple servers — kept as a control group. Override via env var:
#   CRUXIAL_MCP_SERVERS=filesystem,time,fetch,sqlite,everything python ...
SIMPLE_SUBSET = [
    "filesystem", "memory", "time", "fetch", "sqlite",
    "git", "brave-search", "everything",
]


# Curated prompts per server — natural-language requests likely to invoke
# one of the server's tools. Mix of clean + edge-case. ~3 per server.
PROMPTS: dict[str, list[str]] = {
    "filesystem": [
        "List the files in /tmp",
        "Read the contents of /tmp/test.txt",
        "Create a directory at /tmp/cruxial-test",
        "Search for any markdown file in /tmp containing the word 'cruxial'",
    ],
    "memory": [
        "Store a memory: 'Cruxial benchmark run completed on 2026-05-30'",
        "Search my memories for anything mentioning 'cruxial'",
        "Create an entity called 'Cruxial Benchmark' with observation 'Run 1 completed'",
    ],
    "time": [
        "What time is it in Bangalore right now?",
        "Convert 14:30 from America/New_York to Asia/Tokyo",
        "What's the current UTC time?",
    ],
    "fetch": [
        "Fetch the content from https://example.com",
        "Get the page at https://news.ycombinator.com and convert to markdown",
        "Fetch https://anthropic.com/news with max length 5000 chars",
    ],
    "sqlite": [
        "Create a table called users with columns id, email, created_at",
        "Insert a row into users with email 'demo@cruxial.ai'",
        "Show me the schema of all tables",
        "Run SELECT COUNT(*) FROM users",
    ],
    "git": [
        "Show the current git status",
        "Show the last 5 commits",
        "Create a new branch called 'cruxial-mcp-benchmark'",
        "Show the diff for the most recent commit",
    ],
    "brave-search": [
        "Search the web for 'LLM tool call reliability'",
        "Find recent news about Claude 4",
        "Local search for coffee shops in Bangalore",
    ],
    "everything": [
        "Echo back the text 'cruxial test'",
        "Add the numbers 42 and 137",
        "Generate a long output with 5 chunks of 100 chars each",
        "Show me the available environment information",
    ],
    "playwright": [
        "Navigate to https://example.com and take a screenshot",
        "Click the first link on the current page",
        "Fill the search input with 'cruxial'",
    ],
    "github": [
        "List my recent pull requests in the selva-iitg/cruxial repo",
        "Create an issue in selva-iitg/cruxial titled 'MCP benchmark complete'",
        "Search for issues mentioning 'tool call hallucination' across GitHub",
    ],
    "slack": [
        "Post 'deploy starting' to the #engineering channel",
        "List the channels in the workspace",
        "Send a DM to user U12345 saying 'on it'",
    ],
    "postgres": [
        "Show me the schema of the users table in the demo database",
        "Run: SELECT COUNT(*) FROM events WHERE created_at > NOW() - INTERVAL '7 days'",
    ],
    "puppeteer": [
        "Open https://example.com and screenshot it",
        "Get the page title of https://news.ycombinator.com",
    ],

    # ── rich servers — designed to exercise enums/required/nested ─
    "notion": [
        "Create a new Notion database called 'Customer Feedback Q3' with fields for company, feedback, and rating",
        "Append a markdown bullet list of action items to the page 'Sprint Planning'",
        "Search Notion for any page mentioning 'cruxial' or 'tool calling'",
        "Create a comment on the page about Q3 OKRs saying 'reviewed, looks good'",
        "Add a new row to the data source 'Customers' with name='Acme Corp' and status='active'",
    ],
    "kubernetes": [
        "Show me all pods in the 'default' namespace",
        "Scale the deployment 'web-frontend' to 5 replicas in the 'production' namespace",
        "Apply this YAML to the cluster: apiVersion v1, Kind Pod, name test-pod",
        "Delete the pod 'old-worker-xyz' in namespace 'background-jobs'",
        "Switch kubectl context to 'staging-cluster' and list all services",
    ],
    "playwright": [
        "Navigate to https://cruxial.ai and click the 'Get started' button",
        "Take a screenshot of the current page with full page mode enabled",
        "Fill the form with name='Demo User' and email='demo@cruxial.ai'",
        "Wait for the element matching '.success-banner' to appear, then capture network requests",
        "Hover over the third nav menu item and click the dropdown that appears",
    ],
    "firecrawl": [
        "Crawl https://docs.anthropic.com starting from the root, limit 50 pages, output markdown",
        "Extract just the page titles and main headings from https://news.ycombinator.com",
        "Scrape https://example.com with formats markdown and screenshot, mobile viewport",
        "Map all URLs under https://anthropic.com that match the pattern 'news'",
        "Run a search on firecrawl for 'LLM tool calling' and return the top 10 organic results",
    ],
    "airtable": [
        "List all bases in our Airtable workspace",
        "Create a new table called 'Q3 Pipeline' in base 'appXXXabc123' with fields Customer (text), Stage (single-select), Amount (number)",
        "Add a record to the 'Customers' table in base 'appXXXabc123' with name='Acme Corp' and tier='enterprise'",
        "Update record 'recYYYxyz789' in table 'Customers' to set status='active'",
        "Search records in the 'Deals' table where stage equals 'closed-won' for the last 30 days",
    ],
    "github": [
        "Create an issue in selva-iitg/cruxial titled 'V0.1 launched: MCP support' with the body containing details about the release",
        "Open a pull request from feat/mcp-adapter into main in selva-iitg/cruxial with title 'Ship MCP adapter'",
        "Add a review comment on PR #42 in selva-iitg/cruxial: 'Looks good, approved'",
        "Search code in selva-iitg/cruxial for any usage of 'auto_repair_batch'",
        "Create a new branch called 'v0.1-release' from main in selva-iitg/cruxial",
    ],
    "slack": [
        "Post 'Cruxial V0.1 is shipping today, MCP support included' to the #engineering channel",
        "Reply 'on it' to the thread with timestamp 1730000000.123456 in #incidents",
        "Add a thumbs-up reaction to the message at timestamp 1730000000.111111 in #general",
        "Get the recent message history from #product-updates",
        "Look up the user profile for U03ABC123",
    ],

    # ── Comprehensive-benchmark additions ────────────────────────────
    "hubspot": [
        "Create a new HubSpot contact for alice@acme.io, first name Alice, last name Kapoor, company Acme Corp",
        "Find all deals in HubSpot with stage 'qualified-to-buy' closed this quarter",
        "Add a note to contact ID 12345 saying 'Discussed Q3 expansion, follow up next week'",
        "Update the deal 'Acme expansion' to stage 'closed-won' with amount 50000 USD",
    ],
    "linear": [
        "Create a Linear issue in team ENG titled 'Cruxial benchmark sweep complete' priority Medium",
        "Show me all my assigned issues in the ENG team sorted by priority",
        "Add a comment to LIN-1234 saying 'Reviewed, approving'",
        "Create a new project in team ENG called 'Q3 reliability' targeting end of August",
    ],
    "atlassian": [
        "Create a Jira ticket in project ENG titled 'Login fails on Safari 17' as a Bug, priority High",
        "Search for all open issues assigned to me in project PROD",
        "Add a comment to ENG-1234 saying 'Reproduced locally, working on a fix'",
        "Transition ticket PROD-567 to status 'In Progress'",
    ],
    "intercom": [
        "Reply to conversation ID 99999 with the message 'Thanks for reaching out — looking into this now'",
        "Tag the conversation with ID 88888 as 'billing-question'",
        "List all open conversations from the last 24 hours",
        "Search Intercom contacts for anyone at the company 'Acme Corp'",
    ],
    "discord": [
        "Send 'Cruxial V0.1 has launched 🚀' to channel ID 1234567890",
        "List the members of guild 9999999999",
        "Add the 'verified-builder' role to user ID 1111111111 in guild 9999999999",
        "Reply to message ID 5555555555 in channel 1234567890 with a thumbs-up emoji",
    ],
    "supabase": [
        "Create a new Supabase project called 'cruxial-prod' in region us-east-1",
        "List all tables in the database for project ref abcdef123456",
        "Run SQL 'SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL 7 day' on project abcdef123456",
        "Generate TypeScript types for the public schema of project abcdef123456",
    ],
    "mongodb": [
        "Find all documents in the users collection where status equals 'active' limit 25",
        "Insert a document into the events collection: {type: 'login', userId: 'u_123', at: '2026-05-30T...'}",
        "Update all documents in customers where tier='free' to set newsletter=true",
        "Aggregate the orders collection by status grouping count and total amount",
    ],
    "pagerduty": [
        "Create a PagerDuty incident: title 'Payments API total outage', service ID PXXXXXX, urgency high",
        "List incidents triggered in the last hour, status triggered",
        "Acknowledge incident IXXXXXX with the note 'Investigating'",
        "Get the current on-call user for schedule PXXX111",
    ],
    "datadog": [
        "Query Datadog metric avg:trace.web.request.duration{service:payments} for the last hour",
        "Search Datadog logs for ERROR level entries in service auth-service from the last 30 minutes",
        "Create a Datadog monitor on metric avg:system.cpu.user{env:prod} that alerts above 80% for 5 minutes",
        "List my recent dashboards in Datadog",
    ],
    "shopify": [
        "Create a new product in Shopify titled 'Cruxial Sticker Pack' priced at 5 USD, type 'merchandise', inventory 100",
        "List all orders from the last 24 hours that are in fulfillment state 'pending'",
        "Update the inventory level of variant ID 12345678 in location 99999 to 50 units",
        "Search customers whose email contains 'cruxial.ai'",
    ],
    "twilio": [
        "Send an SMS from +15551234567 to +14155550100 with the body 'Your verification code is 123456'",
        "List the 10 most recent calls in our Twilio account",
        "Look up the carrier and line type for phone number +14155550100",
    ],
    "posthog": [
        "Capture an event in PostHog called 'cruxial.benchmark.complete' for distinct_id 'u_demo'",
        "Query the events in PostHog for the last 24 hours grouped by event name",
        "Get the feature flag values for distinct_id 'u_demo'",
        "Create a new cohort in PostHog called 'Active devs' with filter 'event signup in the last 30 days'",
    ],
    "stripe-agent-toolkit": [
        "Create a Stripe customer with email demo@cruxial.ai and name 'Demo Customer'",
        "Issue a $99 invoice in USD to customer cus_demo for 'Pro plan' net 30 terms",
        "Refund $50 from charge ID ch_demo1234",
        "List the 10 most recent successful payments",
    ],
    "spotify": [
        "Search Spotify for the artist 'Tame Impala'",
        "Get the user's currently playing track",
        "Add the track with URI spotify:track:7ouMYWpwJ422jRcDASZB7P to the queue",
    ],
    "vercel": [
        "List all my Vercel projects in team ID team_demo",
        "Trigger a new deployment for project prj_demo in production",
        "Get the latest deployment status for project prj_demo",
    ],
    "context7": [
        "Look up documentation for the React library version 18.3.1",
        "Find docs about Next.js App Router routing and dynamic segments",
        "Get the docs page for FastAPI dependency injection",
    ],
    "youtube-transcript": [
        "Fetch the English transcript for YouTube video ID 'dQw4w9WgXcQ'",
        "Get the auto-generated transcript for video https://www.youtube.com/watch?v=jNQXAC9IVRw in English",
    ],
    "arxiv": [
        "Search arxiv for recent papers about 'LLM tool calling reliability'",
        "Download the PDF of arxiv paper 2510.22977",
        "Find arxiv papers in cs.AI from the last week about agent benchmarks",
    ],
    "wikipedia": [
        "Search Wikipedia for 'Model Context Protocol'",
        "Get the Wikipedia summary of 'Anthropic'",
        "Fetch the full Wikipedia article on 'Tool use (artificial intelligence)'",
    ],
    "youtube": [
        "Search YouTube for videos about 'LLM tool calling'",
        "Get the details of YouTube video ID 'dQw4w9WgXcQ'",
        "List the most recent videos from channel ID UCXuqSBlHAE6Xw-yeJA0Tunw",
    ],
    "perplexity": [
        "Ask perplexity: 'what's the current state of LLM tool calling benchmarks?'",
        "Search perplexity for recent articles about Cruxial.ai",
    ],
    "exa": [
        "Search exa for 'LLM tool call interception layer'",
        "Find recent papers via exa about agent reliability frameworks",
    ],
    "tavily": [
        "Tavily search 'best practices for LLM agent error handling 2026'",
        "Find news via tavily about MCP server adoption",
    ],
    "firecrawl": [
        "Crawl https://docs.anthropic.com starting from the root, limit 50 pages, output markdown",
        "Extract just the page titles and main headings from https://news.ycombinator.com",
        "Scrape https://example.com with formats markdown and screenshot, mobile viewport",
        "Map all URLs under https://anthropic.com that match the pattern 'news'",
        "Run a search on firecrawl for 'LLM tool calling' and return the top 10 organic results",
    ],
    "airtable": [
        "List all bases in our Airtable workspace",
        "Create a new table called 'Q3 Pipeline' in base 'appXXXabc123' with fields Customer (text), Stage (single-select), Amount (number)",
        "Add a record to the 'Customers' table in base 'appXXXabc123' with name='Acme Corp' and tier='enterprise'",
        "Update record 'recYYYxyz789' in table 'Customers' to set status='active'",
        "Search records in the 'Deals' table where stage equals 'closed-won' for the last 30 days",
    ],
    "kubernetes": [
        "Show me all pods in the 'default' namespace",
        "Scale the deployment 'web-frontend' to 5 replicas in the 'production' namespace",
        "Apply this YAML to the cluster: apiVersion v1, Kind Pod, name test-pod",
        "Delete the pod 'old-worker-xyz' in namespace 'background-jobs'",
        "Switch kubectl context to 'staging-cluster' and list all services",
    ],
    "notion": [
        "Create a new Notion database called 'Customer Feedback Q3' with fields for company, feedback, and rating",
        "Append a markdown bullet list of action items to the page 'Sprint Planning'",
        "Search Notion for any page mentioning 'cruxial' or 'tool calling'",
        "Create a comment on the page about Q3 OKRs saying 'reviewed, looks good'",
        "Add a new row to the data source 'Customers' with name='Acme Corp' and status='active'",
    ],
    "playwright": [
        "Navigate to https://cruxial.ai and click the 'Get started' button",
        "Take a screenshot of the current page with full page mode enabled",
        "Fill the form with name='Demo User' and email='demo@cruxial.ai'",
        "Wait for the element matching '.success-banner' to appear, then capture network requests",
        "Hover over the third nav menu item and click the dropdown that appears",
    ],
    "github": [
        "Create an issue in selva-iitg/cruxial titled 'V0.1 launched: MCP support' with the body containing details about the release",
        "Open a pull request from feat/mcp-adapter into main in selva-iitg/cruxial with title 'Ship MCP adapter'",
        "Add a review comment on PR #42 in selva-iitg/cruxial: 'Looks good, approved'",
        "Search code in selva-iitg/cruxial for any usage of 'auto_repair_batch'",
        "Create a new branch called 'v0.1-release' from main in selva-iitg/cruxial",
    ],

    # ── Launch-comprehensive additions ───────────────────────────────
    "trello": [
        "Create a new card on the 'In Progress' list of board ID abc123 titled 'Cruxial benchmark complete', due in 3 days",
        "Move card ID xyz789 to the 'Done' list and add a comment 'Closed by automation'",
        "List all cards in the 'Backlog' list of board abc123 that have the 'urgent' label",
        "Add a checklist 'Pre-launch' with items 'README', 'tests', 'tweet' to card xyz789",
    ],
    "asana": [
        "Create a new Asana task titled 'Review V0.1 launch tweet' assigned to selva@cruxial.ai due tomorrow",
        "Add a comment to Asana task ID 1234567890 saying 'Looks good, approved'",
        "Create a new project in the 'Engineering' team called 'Q3 Reliability' with status 'on track'",
        "Mark Asana task 9999999 as completed and remove the 'in-progress' tag",
    ],
    "zendesk": [
        "Create a Zendesk ticket: subject 'Login failing on Safari 17', requester demo@cruxial.ai, priority high, type problem",
        "Reply to Zendesk ticket ID 12345 with a public comment 'Reproduced — working on fix'",
        "Search Zendesk tickets where status is 'open' and tagged 'billing' in the last 24 hours",
        "Update ticket 99999 to status 'solved' and add internal note 'Fixed in v0.1.1'",
    ],

    # ── 30-server launch run — additional 15 servers ──────────────────
    "gitlab": [
        "Create a new GitLab merge request in project selva-iitg/sdk from feat/runtime-register into main titled 'Add runtime registration'",
        "List all open issues in selva-iitg/sdk assigned to me with label 'bug'",
        "Add a comment to merge request ID 42 in project selva-iitg/sdk: 'Looks good, approving'",
        "Search code in project selva-iitg/sdk for any usage of 'auto_repair_batch'",
        "Create a new branch 'v0.1-release' from main in project selva-iitg/sdk",
    ],
    "circleci": [
        "List recent pipelines for project github/selva-iitg/cruxial in branch main",
        "Get the status of the most recent workflow in project selva-iitg/cruxial",
        "Rerun the failed jobs in workflow ID wf_xyz789",
        "Cancel pipeline ID pipe_abc123 with the reason 'duplicate run'",
    ],
    "docker": [
        "List all running Docker containers",
        "Pull the image 'postgres:16-alpine' from Docker Hub",
        "Stop the container named 'cruxial-db' and remove it",
        "Run a new nginx container exposing port 8080 and name it 'cruxial-web'",
    ],
    "aws": [
        "Search AWS documentation for 'S3 server-side encryption'",
        "Find AWS docs about IAM policy conditions for cross-account access",
        "Look up the AWS documentation page for ECS task definitions",
    ],
    "todoist": [
        "Create a todoist task: 'Ship Cruxial V0.1' due tomorrow at 5pm, priority 4, project Inbox",
        "List all my Todoist tasks due today across all projects",
        "Update task ID 9999999 to set due_string='next Monday' and assignee=demo@cruxial.ai",
        "Add comment 'Blocked on legal review' to task 9999999",
    ],
    "outlook": [
        "Send an email from selva@cruxial.ai to founders@cruxial.ai with subject 'V0.1 launched' and body 'Just shipped Cruxial V0.1 to PyPI'",
        "List the 10 most recent emails in my inbox from the last 24 hours",
        "Search my Outlook calendar for events tomorrow between 9am and 5pm",
        "Create a calendar event 'Sprint review' tomorrow 3pm-4pm UTC, invite alice@cruxial.ai",
        "Reply to message ID AAMkAGUz with body 'Got it, working on this'",
    ],
    "confluence": [
        "Search Confluence for any page mentioning 'cruxial' across all spaces",
        "Get the full content of Confluence page ID 12345 in space ENG",
        "Create a new Confluence page in space ENG titled 'V0.1 Launch Notes' with markdown body",
        "List recent pages in the ENG Confluence space updated in the last week",
    ],
    "figma": [
        "Get the file metadata for Figma file ID abc123xyz",
        "List the comments on Figma file abc123xyz that contain the word 'feedback'",
        "Export the node ID '1:23' from Figma file abc123xyz as a PNG at 2x scale",
        "Create a new comment on Figma file abc123xyz at coordinates x=100, y=200 saying 'Aligned to 8px grid?'",
    ],
    "pinecone": [
        "Create a new Pinecone index named 'cruxial-embeddings' with dimension 1536 and metric cosine",
        "Upsert a vector to index 'cruxial-embeddings' with ID 'doc_1' and values [0.1, 0.2, 0.3] (full 1536-dim)",
        "Query index 'cruxial-embeddings' for the top 10 nearest neighbors of vector [0.1, 0.2, 0.3]",
        "Describe the index stats for 'cruxial-embeddings' in environment us-east-1-aws",
    ],
    "sqlite": [
        "Create a table called 'users' with columns id (integer primary key), email (text unique), created_at (timestamp)",
        "Insert a row into users with email 'demo@cruxial.ai'",
        "Show me the schema of all tables",
        "Run SELECT COUNT(*) FROM users WHERE email LIKE '%cruxial.ai'",
    ],
    "salesforce": [
        "Create a new Salesforce Lead with first name 'Alice', last name 'Kapoor', company 'Acme Corp', email 'alice@acme.io', status 'Open - Not Contacted'",
        "Run a SOQL query to find all Accounts where Industry='Technology' modified in the last 7 days",
        "Update Opportunity ID 0061a000001abc to set StageName='Closed Won' and Amount=50000",
        "Search Salesforce contacts where Email contains '@cruxial.ai'",
    ],
    "ms-teams": [
        "Send a message to Teams channel 'General' in team 'Engineering': 'Cruxial V0.1 shipped'",
        "Schedule a Teams meeting tomorrow 3pm UTC for 30 minutes titled 'V0.1 retro', invite alice@cruxial.ai",
        "List the unread messages in my Teams chats from the last 24 hours",
        "Create a new channel called 'cruxial-launch' in team 'Engineering' with description 'Launch coordination'",
    ],
    "puppeteer": [
        "Navigate the browser to https://cruxial.ai",
        "Take a screenshot of the current page and save as 'cruxial-home.png'",
        "Get the page title and meta description of https://anthropic.com",
        "Click on the element with selector '.cta-primary' on the current page",
    ],
    "browserbase": [
        "Create a new Browserbase session and navigate to https://example.com",
        "Take a screenshot of the current page in session ID sess_xyz789",
        "Click the first link on the current page in session sess_xyz789",
        "Close session sess_xyz789",
    ],
    "everything": [
        "Echo back the text 'cruxial launch test'",
        "Add the numbers 42 and 137",
        "Sample a long-running operation with 5 steps and 10 seconds each",
        "Get the environment information",
        "Return some sample images as resources",
    ],

    # ── Missing 11 servers — all 52-server coverage ───────────────────
    "duckduckgo": [
        "Search DuckDuckGo for 'Cruxial.ai LLM tool calling'",
        "Look up news on DuckDuckGo about 'Claude 4 release'",
    ],
    "loom": [
        "List the 10 most recent Loom recordings in our workspace",
        "Get the metadata for Loom recording ID 'loom_abc123'",
        "Create a new shareable link for Loom recording 'loom_xyz789' that expires in 7 days",
    ],
    "redis": [
        "SET key 'cruxial:status' to 'shipping' with TTL 3600 seconds",
        "GET the value of key 'cruxial:status'",
        "List all keys matching pattern 'cruxial:*' in the database",
        "DELETE the key 'cruxial:temp'",
    ],
    "clickhouse": [
        "Run 'SELECT count() FROM events WHERE event_date >= today() - 7' on the default database",
        "List all tables in the analytics database",
        "Describe the schema of the 'events' table",
    ],
    "neo4j": [
        "Run cypher 'MATCH (n:Person) RETURN n LIMIT 10'",
        "Create a node with label 'Customer' and properties name='Acme', tier='enterprise'",
        "Find all nodes connected to node with id 42 with a 'KNOWS' relationship",
    ],
    "sequentialthinking-tools": [
        "Plan a sequential thinking process for: 'Should we launch Cruxial V0.1 today or wait for V0.2 features?'",
        "Continue the previous reasoning chain with thought #2: 'Consider the cost of delay'",
    ],
    "qdrant": [
        "Store a memory in qdrant: 'Cruxial V0.1 launched on 2026-05-30'",
        "Find similar memories to query 'cruxial benchmark'",
    ],
    "openai-tools": [
        "Generate a chat completion with model gpt-4o-mini and message 'Hello, world'",
    ],
    "pubmed": [
        "Search PubMed for recent papers about 'LLM hallucination in clinical settings'",
    ],
    "sentry": [
        "Get the details of Sentry issue ID 'PROJECT-1234'",
    ],
    "sequential-thinking": [
        "Walk me through your reasoning for whether to ship Cruxial V0.1 today or wait — use 5 thinking steps",
    ],
}


SYSTEM_PROMPT = (
    "You are an assistant with access to a curated set of tools. For every "
    "user request, choose the appropriate tool and call it with reasonable "
    "arguments derived from the message. Always call a tool when one matches "
    "the request — do not ask clarifying questions. Make sensible defaults "
    "for any missing parameter."
)


def _color(text, code):
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def main() -> int:
    missing = [
        v for v in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"error: set {', '.join(missing)} first", file=sys.stderr)
        return 2

    try:
        from cruxial.demo.mcp_schemas import MCP_SCHEMAS
    except ImportError:
        print(
            "error: cruxial.demo.mcp_schemas not found. "
            "Run examples/mine_mcp_schemas.py first.",
            file=sys.stderr,
        )
        return 1

    subset_env = os.environ.get("CRUXIAL_MCP_SERVERS")
    if subset_env:
        subset = [s.strip() for s in subset_env.split(",") if s.strip()]
    else:
        subset = [s for s in DEFAULT_SUBSET if s in MCP_SCHEMAS]

    missing_servers = [s for s in subset if s not in MCP_SCHEMAS]
    if missing_servers:
        print(f"warning: these servers aren't in the snapshot: {missing_servers}", file=sys.stderr)
    subset = [s for s in subset if s in MCP_SCHEMAS]

    if not subset:
        print(
            "error: no servers from the subset are in your snapshot. "
            "Set CRUXIAL_MCP_SERVERS=... or re-run mining.",
            file=sys.stderr,
        )
        return 1

    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    # Build flat schemas + matching OpenAI tools list, ONE per chosen server.
    print(_color(f"\ncruxial MCP suite · {deployment} · {len(subset)} servers\n", "1"))

    stats = {
        "prompts": 0, "tool_calls": 0, "no_tool": 0,
        "passed": 0, "intercepted": 0, "repaired": 0,
        "unrepairable": 0, "api_errors": 0,
    }
    categories: Counter[str] = Counter()
    by_server: dict[str, dict[str, int]] = {
        s: {"prompts": 0, "calls": 0, "intercepted": 0, "repaired": 0}
        for s in subset
    }
    t_start = time.perf_counter()

    for server_id in subset:
        payload = MCP_SCHEMAS[server_id]
        server_schemas = payload["schemas"]
        prompts = PROMPTS.get(server_id, [])

        if not prompts:
            print(_color(f"  ⚠ no prompts curated for {server_id} — skipping", "33"))
            continue
        if not server_schemas:
            print(_color(f"  ⚠ no schemas for {server_id} — skipping", "33"))
            continue

        print(_color(f"\n══ {server_id}  ({len(server_schemas)} tools, {len(prompts)} prompts)", "36"))

        # Build per-server cruxial guard + OpenAI tools list.
        # We use only this server's tools so the model isn't overwhelmed.
        from cruxial.demo import DEMO_TOOL_EXECUTORS_SYNC
        sync_noop = lambda **kw: {"ok": True, "demo": True, "args_echoed": kw}
        executors_for_server = {n: sync_noop for n in server_schemas}

        cruxial = guard(
            schemas=server_schemas,
            executors=executors_for_server,
            config=GuardConfig(sinks=("sqlite",)),
        )
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "",  # MCP servers vary; description not exported in our snapshot
                    "parameters": schema,
                },
            }
            for name, schema in server_schemas.items()
        ]

        for i, prompt_text in enumerate(prompts, 1):
            stats["prompts"] += 1
            by_server[server_id]["prompts"] += 1
            print(f"  [{i}/{len(prompts)}] {prompt_text[:80]}{'…' if len(prompt_text) > 80 else ''}")

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ]
            try:
                resp = client.chat.completions.create(
                    model=deployment, messages=messages, tools=openai_tools,
                )
            except Exception as e:
                stats["api_errors"] += 1
                print(_color(f"    ✗ api error: {str(e)[:100]}", "31"))
                continue

            tool_calls = resp.choices[0].message.tool_calls or []
            if not tool_calls:
                stats["no_tool"] += 1
                txt = (resp.choices[0].message.content or "")[:60]
                print(_color(f"    ⚠ no tool picked. text: {txt}", "33"))
                continue

            assistant_tcs = [
                {
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
            messages_with_assistant = messages + [
                {"role": "assistant", "content": None, "tool_calls": assistant_tcs}
            ]

            outcomes = []
            for tc in tool_calls:
                stats["tool_calls"] += 1
                by_server[server_id]["calls"] += 1
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = cruxial.execute(name, args)
                outcome = {
                    "tool_call_id": tc.id, "name": name, "args": args,
                    "ok": result.ok,
                    "value": result.value if result.ok else None,
                    "failure": result.failure if not result.ok else None,
                    "repair_prompt": (
                        cruxial.build_repair_prompt(result.failure, args)
                        if not result.ok else None
                    ),
                }
                outcomes.append(outcome)

                args_str = json.dumps(args, default=str)[:90]
                if result.ok:
                    stats["passed"] += 1
                    print(_color(f"    → {name}({args_str}) ✓", "32"))
                else:
                    stats["intercepted"] += 1
                    by_server[server_id]["intercepted"] += 1
                    categories[result.failure.category] += 1
                    print(_color(
                        f"    → {name}({args_str}) ✗ {result.failure.category}: "
                        f"{result.failure.message[:80]}",
                        "31",
                    ))

            failed = [o for o in outcomes if not o["ok"]]
            if failed:
                try:
                    corrected = auto_repair_batch(
                        client, model=deployment, messages=messages_with_assistant,
                        tools=openai_tools, tool_call_outcomes=outcomes,
                    )
                except Exception as e:
                    for _ in failed:
                        stats["unrepairable"] += 1
                    print(_color(f"      ↻ batch repair errored: {str(e)[:80]}", "31"))
                    continue

                for o in failed:
                    new_args = corrected.get(o["tool_call_id"])
                    if new_args is None:
                        stats["unrepairable"] += 1
                        continue
                    retry = cruxial.execute_repaired(o["name"], new_args)
                    if retry.ok:
                        stats["repaired"] += 1
                        by_server[server_id]["repaired"] += 1
                        print(_color(f"      ↻ {o['name']} REPAIRED ✓", "32"))
                    else:
                        stats["unrepairable"] += 1
                        print(_color(
                            f"      ↻ {o['name']} repair insufficient: {retry.failure.message[:60]}",
                            "31",
                        ))

        cruxial.close()

    # ─── summary ───────────────────────────────────────────────────
    elapsed = time.perf_counter() - t_start
    tc = stats["tool_calls"] or 1
    print("\n" + "─" * 64)
    print(_color(f"summary · {deployment} · {len(subset)} servers · {elapsed:.0f}s", "1"))
    print("─" * 64)
    print(f"  prompts sent              {stats['prompts']}")
    print(f"  tool calls made           {stats['tool_calls']}")
    print(f"  model declined            {stats['no_tool']}")
    print(f"  api errors                {stats['api_errors']}")
    print()
    print(f"  passed (clean)            {stats['passed']:>4}  ({stats['passed']/tc*100:5.1f}%)")
    print(f"  intercepted               {stats['intercepted']:>4}  ({stats['intercepted']/tc*100:5.1f}%)")
    if stats["intercepted"]:
        print(f"    auto-repaired           {stats['repaired']:>4}  ({stats['repaired']/stats['intercepted']*100:5.1f}% of intercepts)")
        print(f"    unrepairable            {stats['unrepairable']:>4}")
    if categories:
        print("\n  failure categories caught")
        for cat, n in categories.most_common():
            print(f"    {cat:<26} {n}")
    print("\n  by server")
    for sid, d in by_server.items():
        if d["calls"]:
            rate = d["intercepted"] / d["calls"] * 100
            print(f"    {sid:<24} calls {d['calls']:>2}  intercepted {d['intercepted']:>2} ({rate:5.1f}%)  repaired {d['repaired']:>2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
