import assert from "node:assert/strict";

import { AI_GATEWAY_TYPESAFE_BASE_URL, callSystemOne } from "../dist/bridge.js";

async function verifiesGatewayCallAndNativeResult() {
  const nativeResponse = {
    model: "jev-latest",
    answers: {
      action: {
        type: "choice",
        choice: "LONG",
        confidence: 0.82,
        probabilities: { LONG: 0.82, HOLD: 0.18 },
      },
      quality: {
        type: "score",
        score: 0.75,
        confidence: 0.8,
        legend: { 0: "weak", 1: "strong" },
        probabilities: { 0: 0.2, 1: 0.8 },
      },
      tradeWorthy: { type: "noul", noul: 0.77 },
    },
    usage: { input_tokens: 14, output_tokens: 0 },
  };
  let captured;
  const result = await callSystemOne(
    {
      state: { symbol: "TEST", market: "JP" },
      model: "jev-latest",
      questions: {
        action: {
          type: "choice",
          instructions: "Choose an action",
          criteria: { LONG: "Open long", HOLD: "Wait" },
        },
        quality: { type: "score", criteria: ["weak", "strong"] },
        tradeWorthy: { type: "noul", instructions: "Is this trade worthy?" },
      },
    },
    {
      apiKey: "gateway-test-key",
      timeoutMs: 1234,
      fetch: async (input, init) => {
        captured = { url: String(input), init };
        return new Response(JSON.stringify(nativeResponse), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    },
  );

  assert.equal(AI_GATEWAY_TYPESAFE_BASE_URL, "https://ai-gateway.vercel.sh/typesafe");
  assert.equal(captured.url, `${AI_GATEWAY_TYPESAFE_BASE_URL}/v1/systemone`);
  assert.equal(captured.init.headers.Authorization, "Bearer gateway-test-key");
  assert.equal(captured.init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(captured.init.body), {
    state: { symbol: "TEST", market: "JP" },
    model: "jev-latest",
    questions: {
      action: {
        type: "choice",
        instructions: "Choose an action",
        criteria: { LONG: "Open long", HOLD: "Wait" },
      },
      quality: { type: "score", criteria: ["weak", "strong"] },
      tradeWorthy: { type: "noul", instructions: "Is this trade worthy?" },
    },
  });
  assert.deepEqual(result, nativeResponse);
  assert.equal(result.answers.action.choice, "LONG");
  assert.equal(result.answers.action.confidence, 0.82);
  assert.equal(result.answers.action.probabilities.HOLD, 0.18);
  assert.equal(result.answers.tradeWorthy.noul, 0.77);
}

async function verifiesCredentialIsRequired() {
  await assert.rejects(
    callSystemOne(
      {
        state: { symbol: "TEST" },
        questions: { tradeWorthy: { type: "noul" } },
      },
      { apiKey: " " },
    ),
    /Vercel AI Gateway credential is required/,
  );
}

await verifiesGatewayCallAndNativeResult();
await verifiesCredentialIsRequired();
process.stdout.write("Passed 2 TypeSafe SDK bridge checks.\n");
