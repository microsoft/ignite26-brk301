import { parquetMetadataAsync, parquetReadObjects } from "hyparquet";

const chunks = [];
let inputBytes = 0;
try {
  for await (const chunk of process.stdin) {
    inputBytes += chunk.length;
    if (inputBytes > 24 * 1024 * 1024) throw new Error("Input bound exceeded");
    chunks.push(chunk);
  }
  const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (Object.keys(input).length !== 1 || typeof input.parquet_base64 !== "string") {
    throw new Error("Parquet input required");
  }
  const bytes = Buffer.from(input.parquet_base64, "base64");
  if (!bytes.length || bytes.length > 16 * 1024 * 1024 ||
      bytes.toString("base64") !== input.parquet_base64) throw new Error("File bound exceeded");
  const file = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const parsers = {
    timestampFromMilliseconds: value => ({ __timestamp_micros: (value * 1000n).toString() }),
    timestampFromMicroseconds: value => ({ __timestamp_micros: value.toString() }),
    timestampFromNanoseconds: value => {
      if (value % 1000n !== 0n) throw new Error("Sub-microsecond timestamp unsupported");
      return { __timestamp_micros: (value / 1000n).toString() };
    },
  };
  const metadata = await parquetMetadataAsync(file, { parsers });
  if (metadata.num_rows < 0n || metadata.num_rows > 10000n) throw new Error("Row bound exceeded");
  const rows = await parquetReadObjects({ file, metadata, parsers });
  process.stdout.write(JSON.stringify({ rows }, (_key, value) =>
    typeof value === "bigint" ? JSON.rawJSON(value.toString()) : value));
} catch (error) {
  const name = /^[A-Za-z][A-Za-z0-9]*$/.test(error?.name) ? error.name : "Error";
  process.stderr.write(`Parquet decoding failed: ${name}\n`);
  process.exitCode = 1;
}
