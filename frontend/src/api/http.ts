import { PUBLIC_MESSAGES, PublicClientError } from "./errors";

export function requireEventStream(response: Response): ReadableStream<Uint8Array> {
  if (response.status === 422) {
    throw new PublicClientError("validation", PUBLIC_MESSAGES.invalidRequest);
  }
  if (response.status === 503) {
    throw new PublicClientError(
      "service_unavailable",
      PUBLIC_MESSAGES.serviceUnavailable,
    );
  }
  if (!response.ok || !response.body) {
    throw new PublicClientError("http", PUBLIC_MESSAGES.disconnected);
  }
  return response.body;
}
