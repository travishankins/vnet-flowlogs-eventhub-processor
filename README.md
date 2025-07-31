````md
# VNet/NSG Flow Logs → Event Hubs (Azure Functions · Python 3.11)

This Azure Functions app ingests **Azure Network Watcher** flow-log files (NSG/VNet) from a **Storage Account (Blob trigger)**, parses them (v2/v3 formats), and forwards normalized records to **Azure Event Hubs** — using **Managed Identity** (no secrets).

- **Runtime**: Functions v4, Python 3.11
- **Trigger**: Blob Trigger (listens on your flow-log container)
- **Output**: Azure Event Hubs (one JSON event per flow record)
- **Auth**: Managed Identity + RBAC (Blob/Queue data roles and Event Hubs sender)

---

## Table of contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Configure app settings](#configure-app-settings)
4. [Trigger path (adjust if your container differs)](#trigger-path-adjust-if-your-container-differs)
5. [Step 2 — Enable Managed Identity and assign RBAC](#step-2--enable-managed-identity-and-assign-rbac)
   - [Portal steps (quick)](#portal-steps-quick)
   - [CLI steps (repeatable)](#cli-steps-repeatable)
6. [Plan & networking notes](#plan--networking-notes)
7. [Deploy](#deploy)
   - [Option A — Functions Core Tools (recommended)](#option-a--functions-core-tools-recommended)
   - [Option B — Zip deploy (portal or CLI)](#option-b--zip-deploy-portal-or-cli)
8. [Local development](#local-development)
9. [Validation & expected output](#validation--expected-output)
10. [Troubleshooting](#troubleshooting)
11. [FAQ](#faq)
12. [License](#license)

---

## Architecture

- **Producer**: Azure Network Watcher writes hourly **JSON (.json or .json.gz)** flow-log files into a Storage container (commonly `insights-logs-networksecuritygroupflowevent`).
- **Trigger**: Blob trigger fires per new blob.
- **Function**: Decompresses if needed, parses v2/v3 “flowTuples”, normalizes fields (time, protocol, decision, direction), and builds one event per flow record.
- **Sink**: Azure Event Hubs namespace/hub for downstream consumers (Stream Analytics, SIEM, custom processors).

---

## Prerequisites

- An Azure subscription with:
  - A **Storage Account** where flow logs are being written by Network Watcher.
  - An **Event Hubs namespace** and at least one **event hub** to receive events.
  - An **Azure Function App** (Linux).  
    - **Consumption** is fine if your flow-log Storage Account is public (or “Allow Azure services”).  
    - **Premium (EP1+)** required if the Storage Account is **private** (firewall/VNet), so you can use VNet integration + private DNS.
- Local tooling (for Option A):
  - **Python 3.11**
  - **Azure Functions Core Tools v4**
  - **Azure CLI** (if using CLI steps/zip deploy)

---

## Configure app settings

In your **Function App → Configuration → Application settings**, add:

| Setting | Value |
|---|---|
| `FlowLogsStorage__blobServiceUri` | `https://<FLOWLOG_ACCOUNT>.blob.core.windows.net` |
| `FlowLogsStorage__queueServiceUri` | `https://<FLOWLOG_ACCOUNT>.queue.core.windows.net` |
| `FlowLogsStorage__credential` | `managedidentity` |
| `EVENTHUB_FQDN` | `<eventhubs-namespace>.servicebus.windows.net` |
| `EVENTHUB_NAME` | `<your-event-hub-name>` |
| `FUNCTIONS_WORKER_RUNTIME` | `python` |
| `FUNCTIONS_EXTENSION_VERSION` | `~4` |
| *(zip deploy only)* `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |

> The three `FlowLogsStorage__*` settings enable **identity-based connections** for the Blob trigger. Do **not** add a storage connection string for this source.

---

## Trigger path (adjust if your container differs)

The blob trigger path is defined in `FlowLogToEventHub/function.json`. Default:

```json
{
  "scriptFile": "__init__.py",
  "bindings": [
    {
      "type": "blobTrigger",
      "direction": "in",
      "name": "inputBlob",
      "path": "insights-logs-networksecuritygroupflowevent/{*path}",
      "connection": "FlowLogsStorage"
    }
  ]
}
````

* **Change `path`** to the actual container if yours differs (some tenants/experiences use a different container name).
* The `connection` name (`FlowLogsStorage`) must match the prefix of your identity-based settings (`FlowLogsStorage__*`).

---

## Step 2 — Enable Managed Identity and assign RBAC

This function uses a **system-assigned managed identity** from the Function App to access:

* **Flow-log Storage Account** (read blobs; lease/checkpoint behavior may touch queues)
* **Event Hubs** (send events)

### Portal steps (quick)

1. **Function App → Identity → System assigned → On → Save.**
   Copy the **Object (principal) ID** shown.
2. **Storage Account** (that holds the flow logs) → **Access control (IAM)** → **Add role assignment**:

   * **Storage Blob Data Reader** → assign to your Function App’s **managed identity**.
   * *(Optional / environment dependent)* **Storage Queue Data Contributor** → assign to the same identity (use if you see 403s to `queue.core.windows.net` in logs).
3. **Event Hubs namespace** (or the specific hub) → **Access control (IAM)** → **Add role assignment**:

   * **Azure Event Hubs Data Sender** → assign to the same identity.

> **RBAC propagation** can take **2–5 minutes**. Most “trigger didn’t fire” or “403” issues are timing/role-scope related.

### CLI steps (repeatable)

Fill in your resource names and run:

```bash
# Required values
RG=<FUNCTION_APP_RESOURCE_GROUP>            # e.g., event-hub
APP=<FUNCTION_APP_NAME>                     # e.g., travistestfun
FLOWLOG_RG=<FLOWLOG_STORAGE_RG>             # RG of the storage account that stores flow logs
FLOWLOG_ACCOUNT=<FLOWLOG_STORAGE_ACCOUNT>   # e.g., workshopwc
EH_RG=<EVENT_HUBS_RG>                       # RG of the Event Hubs namespace
EH_NAMESPACE=<EVENT_HUBS_NAMESPACE>         # e.g., nw-flowlogs

# 1) Enable system-assigned MI and capture its object id
az functionapp identity assign -g $RG -n $APP
MI_OBJECT_ID=$(az functionapp identity show -g $RG -n $APP --query principalId -o tsv)
echo "Managed identity object id: $MI_OBJECT_ID"

# 2) Grant Storage roles (scope: the flow-log storage account)
FLOWLOG_ID=$(az storage account show -g $FLOWLOG_RG -n $FLOWLOG_ACCOUNT --query id -o tsv)
az role assignment create --assignee-object-id "$MI_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" --scope "$FLOWLOG_ID"

# Optional (add if you see queue 403s in logs for leases/checkpoints)
az role assignment create --assignee-object-id "$MI_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Storage Queue Data Contributor" --scope "$FLOWLOG_ID"

# 3) Grant Event Hubs sender (scope: the EH namespace)
EHNS_ID=$(az eventhubs namespace show -g $EH_RG -n $EH_NAMESPACE --query id -o tsv)
az role assignment create --assignee-object-id "$MI_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure Event Hubs Data Sender" --scope "$EHNS_ID"

# 4) Verify (RBAC can take a few minutes to propagate)
az role assignment list --assignee "$MI_OBJECT_ID" --all -o table
```

---

## Plan & networking notes

* **Public Storage Account**: **Consumption** plan is OK.
* **Private Storage Account** (firewall/VNet only):

  * Use **Functions Premium (EP1+)**
  * Configure **VNet integration**
  * Ensure **private DNS** for `*.blob.core.windows.net` and `*.queue.core.windows.net`
* Event Hubs can be public or private (via Private Link). Ensure outbound connectivity from the Function.

---

## Deploy

> You can use **Core Tools** (recommended for Python builds) or **zip deploy**.

### Option A — Functions Core Tools (recommended)

```bash
# (From repo root)
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

# Publish (replace with your Function App name)
func azure functionapp publish <YOUR_FUNCTION_APP_NAME> --python
```

### Option B — Zip deploy (portal or CLI)

> **Important**: `host.json` must be at the **root** of the zip (not nested in a folder).

**CLI:**

```bash
# Create a clean zip from the project root so host.json is at zip root
zip -r flowlog-forwarder.zip .

# Deploy the zip
az functionapp deployment source config-zip \
  -g <RG> -n <YOUR_FUNCTION_APP_NAME> \
  --src flowlog-forwarder.zip

# Ensure server-side build for Python dependencies
az functionapp config appsettings set -g <RG> -n <YOUR_FUNCTION_APP_NAME> \
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

**Portal:** Function App → **Deployment Center** → **Upload package** → select your zip.
**Kudu:** Function App → **Advanced Tools** → **Go** → **Zip Push Deploy**.

---

## Local development

`local.settings.json` (for local only; **don’t** commit secrets):

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "EVENTHUB_FQDN": "your-namespace.servicebus.windows.net",
    "EVENTHUB_NAME": "nw-flowlogs",
    "MAX_EVENTS_PER_BATCH": "500"
  }
}
```

Run locally:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

---

## Validation & expected output

1. **Confirm Functions discovered your function** (Functions blade shows `FlowLogToEventHub`).
2. **Log streaming**: Function App → **Log streaming**.
3. **Drop a small test blob** in your flow-log container (e.g., `insights-logs-networksecuritygroupflowevent`).
4. **Event Hubs**: Use “Process data” blade or a simple consumer to verify messages.

**Example single event emitted to Event Hubs:**

```json
{
  "time": "2025-07-31T18:30:12Z",
  "srcIp": "10.0.1.4",
  "destIp": "10.2.0.7",
  "srcPort": "443",
  "destPort": "52014",
  "protocol": "TCP",
  "direction": "Inbound",
  "decision": "Allow",
  "flowVersion": 2,
  "resourceId": "/subscriptions/.../networkSecurityGroups/...",
  "category": "NetworkSecurityGroupFlowEvent",
  "rule": "Allow-HTTPS",
  "mac": "000D3A0B5C7E",
  "recordTime": "2025-07-31T18:30:00Z",
  "extras": ["..."]  // optional fields preserved if present
}
```

---

## Troubleshooting

* **Blob trigger never fires**

  * Check `FlowLogsStorage__*` settings and that `function.json`’s `connection` is `"FlowLogsStorage"`.
  * Ensure the Function’s managed identity has **Storage Blob Data Reader** on the **flow-log storage account**.
  * If logs show 403 to `queue.core.windows.net`, add **Storage Queue Data Contributor** on that storage account.
  * Give RBAC **2–5 minutes** to propagate.

* **401/403 sending to Event Hubs**

  * Ensure the managed identity has **Azure Event Hubs Data Sender** on the **namespace** (or the specific hub).

* **Zip deploy succeeded but no function appears**

  * Most common cause is a bad zip layout. Ensure `host.json` and the function folder(s) are at the **zip root**.
  * Confirm runtime settings: `FUNCTIONS_WORKER_RUNTIME=python`, `FUNCTIONS_EXTENSION_VERSION=~4`.

* **Private Storage Account**

  * Use **Premium plan (EP1+)**, enable **VNet integration**, and configure private DNS for `blob` and `queue` endpoints.

---

## FAQ

**Q: Do I need the Queue role?**
A: Many environments only need **Blob Data Reader** on the source account. If you see `queue.core.windows.net` 403s in logs (leases/checkpoints), add **Storage Queue Data Contributor**.

**Q: Can I batch multiple records into one EH message?**
A: This project emits **one event per record** for downstream simplicity. You can adjust batching behavior in `__init__.py` if you prefer.

**Q: Where do I find the Event Hub name?**
A: Inside the Event Hubs namespace, the **child entity** you create is the **hub name** (`EVENTHUB_NAME`). The namespace FQDN goes into `EVENTHUB_FQDN`.

---
