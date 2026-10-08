// Mirrors backend/app/engine/types.py LOGICAL_TYPES exactly — the vocabulary every column-type
// dropdown in the wizard offers. "(auto)" is a UI-only sentinel meaning "let the source suggest it"
// (omits `type` from the ColumnMap sent to the API, so the backend's own default wins).
export const AUTO_TYPE = "(auto)";
export const LOGICAL_TYPES = [
  "SMALLINT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE", "DECIMAL(12,2)", "VARCHAR", "TEXT",
  "BOOLEAN", "DATE", "TIMESTAMP", "TIMESTAMPTZ", "BLOB",
];

// A representative set of valid IANA zones for the time-range bound pickers (any valid IANA name
// works server-side; this list just keeps the dropdown short).
export const TIMEZONES: { v: string; l: string }[] = [
  { v: "UTC", l: "UTC" },
  { v: "Europe/London", l: "Europe/London" },
  { v: "Europe/Berlin", l: "Europe/Berlin" },
  { v: "Asia/Tbilisi", l: "Asia/Tbilisi" },
  { v: "Asia/Dubai", l: "Asia/Dubai" },
  { v: "America/New_York", l: "America/New_York" },
  { v: "America/Los_Angeles", l: "America/Los_Angeles" },
  { v: "Asia/Kolkata", l: "Asia/Kolkata" },
  { v: "Asia/Tokyo", l: "Asia/Tokyo" },
];

export const SUPPORTED_FILE_FORMATS = ["CSV / TSV", "JSON / NDJSON", "Excel .xlsx", "Parquet", ".gz (CSV / JSON)", ".zip (one file)"];
