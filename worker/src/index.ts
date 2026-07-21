export interface Env {
  GEMINI_API_KEY: string;
  RATE_LIMITER: DurableObjectNamespace;
}

const WORKER_RATE_LIMIT = 5;
const WINDOW_MS = 60000;

// Per-client fixed-window counter. Each IP maps to its own Durable Object instance,
// so the cap is enforced here on the server and can't be bypassed from the extension.
export class RateLimiter implements DurableObject {
  private count = 0;
  private windowStart = 0;

  async fetch(_request: Request): Promise<Response> {
    const now = Date.now();
    if (now - this.windowStart >= WINDOW_MS) {
      this.windowStart = now;
      this.count = 0;
    }
    this.count += 1;
    return new Response(JSON.stringify({ allowed: this.count <= WORKER_RATE_LIMIT }), {
      headers: { "Content-Type": "application/json" }
    });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type"
        }
      });
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const ip = request.headers.get("CF-Connecting-IP") ?? "global";
    const limiter = env.RATE_LIMITER.get(env.RATE_LIMITER.idFromName(ip));
    const limitResp = await limiter.fetch("https://rate-limit/check");
    const { allowed } = (await limitResp.json()) as { allowed: boolean };
    if (!allowed) {
      return new Response(JSON.stringify({ error: "rate_limited" }), {
        status: 429,
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*"
        }
      });
    }

    const { image, mimeType, locale } = await request.json() as {
		image: string;
		mimeType: string;
		locale: string;
	};

    const geminiResp = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-api-key": env.GEMINI_API_KEY
        },
        body: JSON.stringify({
          contents: [{
            parts: [
              {
                text: `Look at this image. Determine if it's a meme (has overlaid text, a recognizable meme template, or is clearly satirical/humorous internet content).
				Respond ONLY with JSON in this exact shape, no markdown fences:
				{"isMeme": boolean, "filenameSlug": "short-kebab-case-description", "tags": ["tag1","tag2"]}

				filenameSlug rules:
				- 3-6 words, lowercase, hyphenated.
				- If the image contains visible text, base the slug on that text's meaning and write it in that text's own language and native script (Cyrillic, Arabic, Devanagari, Hangul, etc). Do NOT transliterate or romanize into Latin letters.
				- If there is no visible text in the image, default to this language: ${locale}.
				- Must be safe as a filename (no slashes, colons, or quotes).`
              },
              {
                inline_data: {
                  mime_type: mimeType,
                  data: image
                }
              }
            ]
          }],
          generationConfig: {
            response_mime_type: "application/json"
          }
        })
      }
    );

    interface GeminiResponse {
      candidates?: Array<{
        content?: {
          parts?: Array<{ text?: string }>;
        };
      }>;
    }

    interface ResponseData {
      isMeme: boolean;
      filenameSlug: string;
      tags: string[];
      error?: string;
    }

    const data = await geminiResp.json() as GeminiResponse;

    const rawText = data.candidates?.[0]?.content?.parts?.[0]?.text ?? "{}";

    let memeInfo: ResponseData;

    try {
      memeInfo = JSON.parse(rawText) as ResponseData;
    } catch (error) {
      console.error("JSON parse error:", error);
      memeInfo = { isMeme: false, filenameSlug: "unknown", tags: [], error: "classification_failed" };
    }

    return new Response(JSON.stringify(memeInfo), {
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*"
      }
    });
  }
} satisfies ExportedHandler<Env>;
