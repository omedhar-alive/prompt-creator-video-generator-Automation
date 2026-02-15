import { useState } from "react";
import { createChatkitSession } from "./lib/chatkit";

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

export default function App() {
  // ChatKit
  const [session, setSession] = useState(null);
  const [chatLoading, setChatLoading] = useState(false);

  // Ingest form
  const [brandName, setBrandName] = useState("");
  const [brandDescription, setBrandDescription] = useState("");
  const [productType, setProductType] = useState("");
  const [targetAudience, setTargetAudience] = useState("");
  const [extraComments, setExtraComments] = useState("");
  const [imageFile, setImageFile] = useState(null);

  const [ingestResult, setIngestResult] = useState(null);
  const [ingestLoading, setIngestLoading] = useState(false);

  const startChatkit = async () => {
    setChatLoading(true);
    try {
      const data = await createChatkitSession("omar-test");
      setSession(data);
    } catch (e) {
      console.error(e);
      alert("ChatKit session failed. Check console.");
    } finally {
      setChatLoading(false);
    }
  };

  const ingest = async () => {
    setIngestLoading(true);
    setIngestResult(null);

    try {
      const fd = new FormData();
      fd.append("brand_name", brandName);
      fd.append("brand_description", brandDescription);
      fd.append("product_type", productType);
      fd.append("target_audience", targetAudience);

      // only send if non-empty (so you don't send "null" strings)
      if (extraComments?.trim()) fd.append("extra_comments", extraComments.trim());

      if (imageFile) {
        // backend expects "images" (can be multiple). We'll send one.
        fd.append("images", imageFile);
      }

      const res = await fetch(`${API_BASE}/pipeline/generate`, {
        method: "POST",
        body: fd,
      });

      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`/pipeline/generate failed (${res.status}): ${txt}`);
      }

      const data = await res.json();
      setIngestResult(data);
    } catch (e) {
      console.error(e);
      alert("Ingest failed. Check console.");
    } finally {
      setIngestLoading(false);
    }
  };

  return (
    <div
      style={{
        padding: 24,
        maxWidth: 900,
        margin: "0 auto",
        fontFamily: "system-ui",
      }}
    >
      <h1>AI Video Factory</h1>

      <section
        style={{
          marginTop: 20,
          padding: 16,
          border: "1px solid #ddd",
          borderRadius: 12,
        }}
      >
        <h2>ChatKit</h2>
        <button onClick={startChatkit} disabled={chatLoading}>
          {chatLoading ? "Creating..." : "Start ChatKit Session"}
        </button>

        {session && (
          <pre
            style={{
              marginTop: 12,
              background: "#f7f7f7",
              padding: 12,
              borderRadius: 8,
              overflowX: "auto",
            }}
          >
            {JSON.stringify(session, null, 2)}
          </pre>
        )}
      </section>

      <section
        style={{
          marginTop: 20,
          padding: 16,
          border: "1px solid #ddd",
          borderRadius: 12,
        }}
      >
        <h2>Step 1: Ingest Brand + Image</h2>

        <div style={{ display: "grid", gap: 10 }}>
          <label>
            Brand Name
            <input
              value={brandName}
              onChange={(e) => setBrandName(e.target.value)}
              style={{ width: "100%", padding: 10, marginTop: 6 }}
            />
          </label>

          <label>
            Brand Description
            <textarea
              value={brandDescription}
              onChange={(e) => setBrandDescription(e.target.value)}
              rows={3}
              style={{ width: "100%", padding: 10, marginTop: 6 }}
            />
          </label>

          <label>
            Product Type
            <input
              value={productType}
              onChange={(e) => setProductType(e.target.value)}
              style={{ width: "100%", padding: 10, marginTop: 6 }}
            />
          </label>

          <label>
            Target Audience
            <input
              value={targetAudience}
              onChange={(e) => setTargetAudience(e.target.value)}
              style={{ width: "100%", padding: 10, marginTop: 6 }}
            />
          </label>

          <label>
            Extra Comments (optional)
            <textarea
              value={extraComments}
              onChange={(e) => setExtraComments(e.target.value)}
              rows={2}
              style={{ width: "100%", padding: 10, marginTop: 6 }}
            />
          </label>

          <label>
            Choose Image (optional)
            <input
              type="file"
              accept="image/*"
              onChange={(e) => setImageFile(e.target.files?.[0] || null)}
              style={{ display: "block", marginTop: 6 }}
            />
          </label>

          <button onClick={ingest} disabled={ingestLoading}>
            {ingestLoading ? "Uploading..." : "Generate: Pipeline (Ingest + Concept + Runway)"}
          </button>
        </div>

        {ingestResult && (
          <>
            <pre
              style={{
                marginTop: 12,
                background: "#f7f7f7",
                padding: 12,
                borderRadius: 8,
                overflowX: "auto",
              }}
            >
              {JSON.stringify(ingestResult, null, 2)}
            </pre>

            {ingestResult?.runway?.status === "SUCCEEDED" &&
              ingestResult?.runway?.video_url && (
                <div style={{ marginTop: 20 }}>
                  <h3>Generated Video</h3>

                  <video
                    controls
                    width="480"
                    src={ingestResult.runway.video_url}
                    style={{ borderRadius: 12 }}
                  />
                </div>
              )}
          </>
        )}
      </section>
    </div>
  );
}
