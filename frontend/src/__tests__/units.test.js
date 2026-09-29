/* Unit tests for the pure functions behind the UI primitives.
 *
 * These are the parts with edge cases a browser test will not catch reliably:
 * lexicographic-vs-numeric sorting, CSV escaping, and subsequence matching. The
 * screens themselves are verified by driving a real browser; this pins the logic
 * underneath them.
 */
import { describe, expect, it } from "vitest";
import { compare, toCsv } from "../components/DataTable.jsx";
import { fuzzyScore } from "../components/CommandPalette.jsx";

describe("compare", () => {
  it("orders numeric strings numerically, not lexicographically", () => {
    // Positive means 100 sorts AFTER 20, which is the numeric answer. A
    // lexicographic comparator returns negative here, putting 100 first.
    expect(compare("100", "20")).toBeGreaterThan(0);
    expect(["300", "150", "80", "220", "60"].sort(compare))
      .toEqual(["60", "80", "150", "220", "300"]);
  });

  it("orders real numbers", () => {
    expect(compare(1, 2)).toBeLessThan(0);
    expect(compare(2.5, 2.4)).toBeGreaterThan(0);
    expect(compare(0, 0)).toBe(0);
  });

  it("sorts nulls last regardless of direction", () => {
    expect(compare(null, 5)).toBeGreaterThan(0);
    expect(compare(5, null)).toBeLessThan(0);
    expect(compare(null, null)).toBe(0);
  });

  it("falls back to locale comparison for non-numeric text", () => {
    expect(compare("AAPL", "MSFT")).toBeLessThan(0);
    expect(compare("filled", "cancelled")).toBeGreaterThan(0);
  });

  it("treats empty strings as text, not zero", () => {
    // "" coerces to 0 under Number(), which would sort it among the numbers.
    expect(compare("", "5")).not.toBe(-5);
  });
});

describe("toCsv", () => {
  const cols = [{ key: "sym", header: "Symbol" }, { key: "qty", header: "Qty" }];

  it("writes a header row and CRLF line endings", () => {
    const csv = toCsv(cols, [{ sym: "AAPL", qty: 100 }]);
    expect(csv.split("\r\n")[0]).toBe('"Symbol","Qty"');
    expect(csv).toContain("\r\n");
  });

  it("quotes values containing commas so columns do not shift", () => {
    // An instrument name with a comma silently shifts every later column in a
    // file someone reconciles against.
    const csv = toCsv(cols, [{ sym: "ACME, Inc.", qty: 5 }]);
    expect(csv).toContain('"ACME, Inc.","5"');
    expect(csv.split("\r\n")[1].split('","').length).toBe(2);
  });

  it("doubles internal quotes", () => {
    const csv = toCsv(cols, [{ sym: 'AA"PL', qty: 1 }]);
    expect(csv).toContain('"AA""PL"');
  });

  it("renders null and undefined as empty, not as the words", () => {
    const csv = toCsv(cols, [{ sym: null, qty: undefined }]);
    expect(csv.split("\r\n")[1]).toBe('"",""');
  });

  it("uses the raw accessor when a column defines one", () => {
    const c = [{ key: "side", header: "Side", raw: (r) => (r.side === "1" ? "BUY" : "SELL") }];
    expect(toCsv(c, [{ side: "1" }])).toContain('"BUY"');
  });
});

describe("fuzzyScore", () => {
  it("matches a subsequence, not just a substring", () => {
    expect(fuzzyScore("Performance Lab", "pfl")).not.toBeNull();
    expect(fuzzyScore("Trading Desk", "tdk")).not.toBeNull();
  });

  it("returns null when the characters are not all present in order", () => {
    expect(fuzzyScore("Performance Lab", "zzz")).toBeNull();
    expect(fuzzyScore("Trading Desk", "dt")).toBeNull();   // right letters, wrong order
  });

  it("scores an empty query as a match for everything", () => {
    expect(fuzzyScore("anything", "")).toBe(0);
  });

  it("prefers an earlier, tighter match", () => {
    const tight = fuzzyScore("Market Risk", "mar");
    const loose = fuzzyScore("Smart Order Routing", "mar");
    expect(tight).toBeLessThan(loose);
  });

  it("prefers a word-boundary start over a mid-word hit", () => {
    const boundary = fuzzyScore("Risk Copilot", "co");
    const midword = fuzzyScore("Reconciliation", "co");
    expect(boundary).toBeLessThan(midword);
  });

  it("ignores case and whitespace in the query", () => {
    expect(fuzzyScore("Performance Lab", "PL")).not.toBeNull();
    expect(fuzzyScore("Performance Lab", "p l")).not.toBeNull();
  });
});
