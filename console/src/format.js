// Indian digit grouping: 18400 becomes ₹18,400 and 185000 becomes ₹1,85,000.
export function inr(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const [whole, frac] = Math.abs(n).toFixed(2).split(".");
  const head = whole.slice(0, -3);
  const tail = whole.slice(-3);
  const grouped = head ? head.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + tail : tail;
  return `${n < 0 ? "-" : ""}₹${grouped}${frac === "00" ? "" : "." + frac}`;
}

export const pct = (x) => (x === null || x === undefined ? "—" : `${Math.round(x * 1000) / 10}%`);
export const two = (x) => (x === null || x === undefined ? "—" : Number(x).toFixed(2));

export function clock(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour12: false });
}

export function ago(epochSeconds) {
  if (!epochSeconds) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export const seconds = (x) => (x === null || x === undefined ? "—" : `${x} s`);
export const shortId = (id) => (id ? id.split("-").pop().slice(0, 6) : "");
