import { expect, it } from "vitest";
import {
  resourceFiles,
  resourceZip,
  validateResourceSetup,
  type ResourceSetup,
} from "../src/lib/resource-package";

const setup: ResourceSetup = {
  resourceType: "local_docker",
  kernelUrl: "http://host.docker.internal:38182",
  provider: "demo",
  model: "demo-pi",
  baseUrl: "http://host.docker.internal:38183/v1",
};
const credential = {
  id: "11111111-1111-4111-8111-111111111111",
  token: "test-token",
};

it("exports a self-contained compose package without model secrets or host mounts", () => {
  const files = resourceFiles(setup, credential);
  expect(files[".env"]).toContain("NIUCAI_PI_PROVIDER_KEY=''\n");
  expect(files["compose.yml"]).toContain("${NIUCAI_SOURCE_PATH:-../..}");
  expect(files["compose.yml"]).toContain("node-data:/data");
  expect(files["compose.yml"]).not.toMatch(/docker.sock|ports:/);
  expect(files["README.txt"]).toContain("PowerShell");
  expect(
    resourceFiles(
      {
        ...setup,
        resourceType: "remote_vps",
        kernelUrl: "https://kernel.example.com",
      },
      credential,
    )["README.txt"],
  ).toContain("VPS 终端");
  const zip = resourceZip(files);
  expect(new DataView(zip.buffer).getUint32(0, true)).toBe(0x04034b50);
});

it("rejects unreachable loopback Kernel addresses and credential interpolation", () => {
  for (const kernelUrl of [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
    "https://user:password@example.com",
    "https://example.com/$TOKEN",
    "file:///etc/passwd",
  ]) {
    expect(() => validateResourceSetup({ ...setup, kernelUrl })).toThrow();
  }
  expect(() =>
    validateResourceSetup({ ...setup, resourceType: "remote_vps" }),
  ).toThrow(/HTTPS/);
  expect(() =>
    resourceFiles({ ...setup, model: "model\nINJECT=value" }, credential),
  ).toThrow();
});

it("uses the managed network independently of Desktop management URL", () => {
  const files = resourceFiles(
    {
      ...setup,
      connectionMode: "compose",
      kernelUrl: "http://api:8080",
      managementUrl: "http://127.0.0.1:8080",
    },
    credential,
  );
  expect(files["compose.yml"]).toContain("external: true");
  expect(files["compose.yml"]).toContain("name: niucai-managed");
  expect(files[".env"]).toContain("http://api:8080");
});

it("permits protected relay loopback without weakening public HTTPS validation", () => {
  const files = resourceFiles(
    {
      ...setup,
      resourceType: "remote_vps",
      connectionMode: "ssh_reverse",
      kernelUrl: "http://127.0.0.1:18080",
    },
    credential,
  );
  expect(files["compose.yml"]).toContain('network_mode: "service:relay"');
  expect(files["compose.yml"]).toContain("UNIX-CONNECT:/tunnel/kernel.sock");
  expect(files["Dockerfile.relay"]).toContain("socat");
  for (const kernelUrl of [
    "http://101.126.159.10:18080",
    "https://127.0.0.1:18080",
    "http://user:password@127.0.0.1:18080",
    "http://127.0.0.1:18080/$TOKEN",
  ])
    expect(() =>
      validateResourceSetup({
        ...setup,
        connectionMode: "ssh_reverse",
        kernelUrl,
      }),
    ).toThrow();
  expect(() =>
    resourceFiles({ ...setup, kernelInstanceId: "x'\nSECRET=1" }, credential),
  ).toThrow();
});
