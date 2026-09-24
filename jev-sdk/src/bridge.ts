import { TypeSafeClient } from "@typesafe-ai/sdk";
import { pathToFileURL } from "node:url";
import type {
  Questions,
  SystemOneRequest,
  SystemOneResult,
  TypeSafeClientConfig,
} from "@typesafe-ai/sdk";

export const AI_GATEWAY_TYPESAFE_BASE_URL = "https://ai-gateway.vercel.sh/typesafe";

interface BridgeEnvelope {
  request: SystemOneRequest;
  timeout_ms: number;
  max_response_bytes: number;
}

interface GatewayClientOptions {
  apiKey?: string;
  timeoutMs?: number;
  fetch?: NonNullable<TypeSafeClientConfig["fetch"]>;
}

type Question = SystemOneRequest["questions"][string];

export async function callSystemOne(
  request: SystemOneRequest,
  options: GatewayClientOptions = {},
): Promise<SystemOneResult<Questions>> {
  const apiKey =
    options.apiKey ?? process.env.AI_GATEWAY_API_KEY ?? process.env.VERCEL_OIDC_TOKEN;
  if (!apiKey?.trim()) {
    throw new Error("Vercel AI Gateway credential is required");
  }

  const timeoutMs = options.timeoutMs ?? 10_000;
  const clientConfig: TypeSafeClientConfig = {
    apiKey,
    baseURL: AI_GATEWAY_TYPESAFE_BASE_URL,
    logLevel: "off",
    retry: { maxRetries: 0, apiConnectionError: false, apiTimeoutError: false },
    timeout: timeoutMs,
    ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
  };
  const client = new TypeSafeClient(clientConfig);
  return client.systemOne(request, {
    timeout: timeoutMs,
    retry: { maxRetries: 0, apiConnectionError: false, apiTimeoutError: false },
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isJsonValue(value: unknown): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (Array.isArray(value)) {
    return value.every(isJsonValue);
  }
  return isRecord(value) && Object.values(value).every(isJsonValue);
}

function isEntryType(value: unknown): value is SystemOneRequest["state"] {
  return (
    typeof value === "string" ||
    (Array.isArray(value) && value.every(isJsonValue)) ||
    (isRecord(value) && Object.values(value).every(isJsonValue)) ||
    value === null
  );
}

function isQuestion(value: unknown): value is Question {
  if (!isRecord(value)) {
    return false;
  }
  if (value.instructions !== undefined && !isEntryType(value.instructions)) {
    return false;
  }
  if (value.type === "choice") {
    return (
      isRecord(value.criteria) &&
      Object.keys(value.criteria).length >= 2 &&
      Object.values(value.criteria).every(isEntryType)
    );
  }
  if (value.type === "score") {
    return (
      Array.isArray(value.criteria) &&
      value.criteria.length >= 2 &&
      value.criteria.every(isEntryType)
    );
  }
  if (value.type === "noul") {
    if (value.criteria === undefined || value.criteria === null) {
      return true;
    }
    return (
      isRecord(value.criteria) &&
      (value.criteria.true === undefined || isEntryType(value.criteria.true)) &&
      (value.criteria.false === undefined || isEntryType(value.criteria.false))
    );
  }
  return false;
}

function isQuestions(value: unknown): value is Questions {
  return isRecord(value) && Object.keys(value).length > 0 && Object.values(value).every(isQuestion);
}

function parseEnvelope(value: unknown): BridgeEnvelope {
  if (!isRecord(value) || !isRecord(value.request)) {
    throw new Error("Bridge input must contain a request object");
  }
  const request = value.request;
  if (
    !isEntryType(request.state) ||
    !isQuestions(request.questions) ||
    (request.model !== undefined &&
      (typeof request.model !== "string" || request.model.trim().length === 0))
  ) {
    throw new Error("Bridge request is not a valid TypeSafe System One request");
  }
  const model = typeof request.model === "string" ? request.model : undefined;
  const timeoutMs = value.timeout_ms;
  const maxResponseBytes = value.max_response_bytes;
  if (
    typeof timeoutMs !== "number" ||
    !Number.isInteger(timeoutMs) ||
    timeoutMs <= 0 ||
    typeof maxResponseBytes !== "number" ||
    !Number.isInteger(maxResponseBytes) ||
    maxResponseBytes <= 0
  ) {
    throw new Error("Bridge timeout and response limit must be positive integers");
  }
  return {
    request: {
      state: request.state,
      questions: request.questions,
      ...(model === undefined ? {} : { model }),
    },
    timeout_ms: timeoutMs,
    max_response_bytes: maxResponseBytes,
  };
}

async function readInput(): Promise<string> {
  const chunks: Uint8Array[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function main(): Promise<void> {
  try {
    const envelope = parseEnvelope(JSON.parse(await readInput()) as unknown);
    const result = await callSystemOne(envelope.request, { timeoutMs: envelope.timeout_ms });
    const output = JSON.stringify(result);
    if (Buffer.byteLength(output, "utf8") > envelope.max_response_bytes) {
      throw new Error("TypeSafe response exceeded the configured size limit");
    }
    process.stdout.write(output);
  } catch (error) {
    const errorName = error instanceof Error ? error.name : "Error";
    process.stderr.write(`TypeSafe SDK request failed (${errorName})\n`);
    process.exitCode = 1;
  }
}

if (
  process.argv[1] !== undefined &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  void main();
}
