package main

// ABOUTME: Known-answer test for the Claude Desktop bearer token against botocore's SigV4QueryAuth.
// ABOUTME: Regenerate the expected token with testdata/gen_bedrock_token_vector.py.

import (
	"encoding/base64"
	"net/url"
	"strings"
	"testing"
	"time"
)

// botocoreVectorSignature is the X-Amz-Signature that botocore 1.43.104's SigV4QueryAuth (the signer
// aws-bedrock-token-generator uses) produces for the inputs in TestGenerateBedrockTokenMatchesBotocore.
// Regenerate with testdata/gen_bedrock_token_vector.py. Only the signature is pinned, not an encoded
// token, so secret scanners have nothing token-shaped to match.
const botocoreVectorSignature = "6a2a1db819c766f926adc98f6b7c84a2294ffd0dc0b5717543eeccd1c2e46a8f"

func decodeBearerToken(t *testing.T, token string) (string, url.Values) {
	t.Helper()
	if !strings.HasPrefix(token, authPrefix) {
		t.Fatalf("token lacks %q prefix", authPrefix)
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(token, authPrefix))
	if err != nil {
		t.Fatalf("token is not base64: %v", err)
	}
	host, query, ok := strings.Cut(string(raw), "/?")
	if !ok {
		t.Fatalf("token payload %q has no /? separator", raw)
	}
	values, err := url.ParseQuery(query)
	if err != nil {
		t.Fatalf("token query does not parse: %v", err)
	}
	return host, values
}

// TestGenerateBedrockTokenMatchesBotocore pins the signature to botocore's. The earlier
// implementation signed "UNSIGNED-PAYLOAD", which Bedrock rejects with "Authentication failed".
func TestGenerateBedrockTokenMatchesBotocore(t *testing.T) {
	at := time.Date(2026, 10, 27, 10, 0, 0, 0, time.UTC)
	sessionToken := "IQoJb3JpZ2luX2VjEXAMPLE/token+with=chars"
	got, err := generateBedrockTokenAt("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
		sessionToken, "us-east-1", at)
	if err != nil {
		t.Fatalf("generateBedrockTokenAt: %v", err)
	}
	gotHost, gotQuery := decodeBearerToken(t, got)
	if gotHost != bedrockHost {
		t.Errorf("host = %q, want %q", gotHost, bedrockHost)
	}
	// Parameter order differs (Go sorts, botocore appends), so compare the decoded parameters.
	want := url.Values{
		"Action":               {"CallWithBearerToken"},
		"X-Amz-Algorithm":      {"AWS4-HMAC-SHA256"},
		"X-Amz-Credential":     {"AKIDEXAMPLE/20261027/us-east-1/bedrock/aws4_request"},
		"X-Amz-Date":           {"20261027T100000Z"},
		"X-Amz-Expires":        {"43200"},
		"X-Amz-SignedHeaders":  {"host"},
		"X-Amz-Security-Token": {sessionToken},
		"X-Amz-Signature":      {botocoreVectorSignature},
		"Version":              {"1"},
	}
	for key, w := range want {
		if g := gotQuery[key]; len(g) != 1 || g[0] != w[0] {
			t.Errorf("%s = %v, want %v", key, g, w)
		}
	}
	if len(gotQuery) != len(want) {
		t.Errorf("parameter count = %d, want %d (%v)", len(gotQuery), len(want), gotQuery)
	}
}

func TestGenerateBedrockTokenRequiresCredentialsAndRegion(t *testing.T) {
	cases := [][3]string{{"", "secret", "us-east-1"}, {"AKID", "", "us-east-1"}, {"AKID", "secret", ""}}
	for _, c := range cases {
		if _, err := generateBedrockToken(c[0], c[1], "", c[2]); err == nil {
			t.Errorf("generateBedrockToken(%q, %q, %q) returned no error", c[0], c[1], c[2])
		}
	}
}
