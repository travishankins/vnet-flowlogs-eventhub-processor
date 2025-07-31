import io
import os
import gzip
import json
import logging
import atexit
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Union

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.eventhub import EventHubProducerClient, EventData

# Environment variables (set in Function App configuration)
EVENTHUB_FQDN = os.getenv("EVENTHUB_FQDN")         # e.g., "mynamespace.servicebus.windows.net"
EVENTHUB_NAME = os.getenv("EVENTHUB_NAME")         # e.g., "nw-flowlogs"
MAX_EVENTS_PER_BATCH = int(os.getenv("MAX_EVENTS_PER_BATCH", "500"))

# Validate required environment variables
if not EVENTHUB_FQDN or not EVENTHUB_NAME:
    raise ValueError("EVENTHUB_FQDN and EVENTHUB_NAME environment variables must be set")

# Create a single, long-lived producer client (Functions worker process reuses this across invocations)
_producer: Optional[EventHubProducerClient] = None

def _get_producer() -> EventHubProducerClient:
    """Gets or creates a long-lived Event Hub producer client."""
    global _producer
    if _producer is None:
        try:
            cred = DefaultAzureCredential()
            _producer = EventHubProducerClient(
                fully_qualified_namespace=EVENTHUB_FQDN,
                eventhub_name=EVENTHUB_NAME,
                credential=cred
            )
            logging.info("Created new Event Hub producer client")
        except Exception as e:
            logging.error("Failed to create Event Hub producer client: %s", e)
            raise
    return _producer

def _cleanup_producer() -> None:
    """Cleanup producer on function app shutdown"""
    global _producer
    if _producer:
        try:
            _producer.close()
            logging.info("Event Hub producer client closed")
        except Exception as e:
            logging.warning("Error closing producer client: %s", e)
        _producer = None

# Register cleanup handler
atexit.register(_cleanup_producer)

def _is_gzip(name: str, first_bytes: bytes) -> bool:
    """Check if the blob is gzipped based on filename or magic bytes"""
    if name.endswith(".gz"):
        return True
    return len(first_bytes) >= 2 and first_bytes[0] == 0x1F and first_bytes[1] == 0x8B

def _read_blob_bytes(input_blob: bytes, name: str) -> bytes:
    """Read blob bytes, decompressing if gzipped"""
    head = input_blob[:2] if input_blob else b""
    if _is_gzip(name, head):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(input_blob), mode="rb") as f:
                return f.read()
        except Exception as e:
            logging.error("Failed to decompress gzipped blob %s: %s", name, e)
            raise
    return input_blob

def _to_iso8601(ts: Union[int, float, str]) -> Optional[str]:
    """
    Convert timestamp to ISO8601 format.
    Handles integer/float Unix timestamps and string timestamps.
    """
    if isinstance(ts, (int, float)):
        try:
            # Using datetime.fromtimestamp with timezone for modern approach
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError) as e:
            logging.warning("Invalid Unix timestamp %s: %s", ts, e)
            return None
    if isinstance(ts, str):
        return ts
    return None

def _parse_flow_tuples(flow_tuples: List[str], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse flow tuples from NSG flow logs.
    flow_tuples are comma-separated strings.
    """
    results = []
    protocol_map = {"T": "TCP", "U": "UDP", "I": "ICMP"}
    direction_map = {"I": "Inbound", "O": "Outbound", "U": "Unknown"}
    decision_map = {"A": "Allow", "D": "Deny"}
    
    for t in flow_tuples:
        parts = [p.strip() for p in t.split(",")]
        # Minimum expected parts for v2/v3 is 8
        if len(parts) < 8:
            logging.warning("Skipping malformed flow tuple with %d parts: '%s'", len(parts), t)
            continue

        try:
            record = {
                **common,
                "time": _to_iso8601(int(parts[0])) if parts[0].isdigit() else _to_iso8601(parts[0]),
                "srcIp": parts[1],
                "destIp": parts[2],
                "srcPort": parts[3],
                "destPort": parts[4],
                "protocol": protocol_map.get(parts[5], parts[5]),
                "direction": direction_map.get(parts[6], parts[6]),
                "decision": decision_map.get(parts[7], parts[7]),
            }
            if len(parts) > 8:
                # Add any extra fields found in the tuple as a list
                record["extraFields"] = parts[8:]
            results.append(record)
        except Exception as e:
            logging.warning("Error parsing flow tuple '%s': %s. Record will be skipped.", t, e)
            continue
            
    return results

def _flatten_records(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse NSG flow log records from JSON document.
    Handles both v2 and v3-like shapes.
    """
    out = []
    records = doc.get("records", [])
    
    if not records:
        logging.warning("No 'records' array found in document")
        return out
    
    for rec in records:
        props = rec.get("properties", {})
        version = props.get("Version") or props.get("version") or props.get("V") or "v2"
        resource_id = rec.get("resourceId")
        category = rec.get("category")
        rec_time = rec.get("time")

        flows_root = props.get("flows", [])
        for rule_block in flows_root:
            rule_name = rule_block.get("rule") or rule_block.get("ruleName")
            inner_flows = rule_block.get("flows", [])

            for f in inner_flows:
                mac = f.get("mac")
                flow_tuples = f.get("flowTuples", [])
                common = {
                    "flowVersion": version,
                    "resourceId": resource_id,
                    "category": category,
                    "rule": rule_name,
                    "mac": mac,
                    "recordTime": rec_time
                }
                out.extend(_parse_flow_tuples(flow_tuples, common))

        # Fallback for some v3 shapes where flowTuples are directly in properties
        if not flows_root and "flowTuples" in props:
            common = {
                "flowVersion": version,
                "resourceId": resource_id,
                "category": category,
                "recordTime": rec_time
            }
            out.extend(_parse_flow_tuples(props.get("flowTuples", []), common))

    return out

def _iter_events_from_blob(blob_bytes: bytes, blob_name: str) -> List[EventData]:
    """Parse blob content and convert to Event Hub events"""
    try:
        text = _read_blob_bytes(blob_bytes, blob_name).decode("utf-8")
        if not text.strip():
            logging.warning("Blob %s is empty after decompression", blob_name)
            return []
            
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        logging.error("Failed to parse blob %s as JSON: %s", blob_name, e)
        return []
    except Exception as e:
        logging.error("Failed to process blob %s: %s", blob_name, e, exc_info=True)
        return []

    records = _flatten_records(doc)
    events = []
    
    for r in records:
        try:
            event_data = json.dumps(r, ensure_ascii=False)
            events.append(EventData(event_data))
        except Exception as e:
            logging.warning("Failed to serialize record to JSON: %s", e)
            continue
            
    return events

def _send_in_batches(producer: EventHubProducerClient, events: List[EventData]) -> int:
    """Send events to Event Hub in batches"""
    if not events:
        logging.info("No events to send")
        return 0
    
    sent = 0
    try:
        batch = producer.create_batch()
        
        for ev in events:
            try:
                batch.add(ev)
            except ValueError:
                # Batch is full, send it
                if len(batch) > 0:
                    producer.send_batch(batch)
                    sent += len(batch)
                    logging.debug("Sent batch of %d events", len(batch))
                
                # Create new batch and try to add the event
                batch = producer.create_batch()
                try:
                    batch.add(ev)
                except ValueError:
                    # This event is too large to fit in a batch, even alone.
                    logging.error("Event too large to fit in a batch (size: %d bytes), skipping.", len(ev.body))
                    continue
            
            # Send batch if it reaches max size
            if len(batch) >= MAX_EVENTS_PER_BATCH:
                producer.send_batch(batch)
                sent += len(batch)
                logging.debug("Sent batch of %d events", len(batch))
                batch = producer.create_batch()
        
        # Send remaining events in batch
        if len(batch) > 0:
            producer.send_batch(batch)
            sent += len(batch)
            logging.debug("Sent final batch of %d events", len(batch))
            
    except Exception as e:
        logging.error("Failed to send events to Event Hub: %s", e, exc_info=True)
        raise
    
    return sent

def main(inputBlob: func.InputStream) -> None:
    """Main Azure Function entry point"""
    blob_name = inputBlob.name
    blob_len = inputBlob.length
    logging.info("Processing flow-log blob: %s (%d bytes)", blob_name, blob_len)

    try:
        # Read blob data
        data = inputBlob.read()
        if not data:
            logging.warning("Blob %s is empty", blob_name)
            return

        # Parse events from blob
        events = _iter_events_from_blob(data, blob_name)
        logging.info("Parsed %d flow records from %s", len(events), blob_name)

        if not events:
            logging.info("No events to send from blob %s", blob_name)
            return

        # Send events to Event Hub
        producer = _get_producer()
        sent = _send_in_batches(producer, events)
        logging.info("Successfully sent %d events to Event Hub '%s' from blob %s", 
                    sent, EVENTHUB_NAME, blob_name)
        
    except Exception as e:
        logging.error("Failed to process blob %s: %s", blob_name, e, exc_info=True)
        raise