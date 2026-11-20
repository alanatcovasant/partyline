import { useState } from "react";

// A copy-to-clipboard code block used across the Connect-an-agent view. Multi-line code
// renders in a scrollable <pre>; the button copies the raw text.
export function Snippet({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="snippet">
      {lang && <span className="snippet-lang">{lang}</span>}
      <button
        type="button"
        className="copy-btn"
        title="Copy to clipboard"
        onClick={() =>
          navigator.clipboard?.writeText(code).then(
            () => { setCopied(true); window.setTimeout(() => setCopied(false), 1200); },
            () => {},
          )
        }
      >
        {copied ? "copied ✓" : "copy"}
      </button>
      <pre><code>{code}</code></pre>
    </div>
  );
}
