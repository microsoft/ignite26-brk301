import { parquetWriteBuffer, schemaFromColumnData } from "hyparquet-writer";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const columnData = input.columns.map(column => ({
  name: column.name,
  type: column.type,
  data: column.data.map(value => value !== null && column.type === "INT64" ? BigInt(value) : value),
}));
const schema = schemaFromColumnData({ columnData });
for (const column of input.columns) {
  if (column.timestamp_unit) {
    const field = schema.find(field => field.name === column.name);
    field.logical_type = { type: "TIMESTAMP", isAdjustedToUTC: true, unit: column.timestamp_unit };
  }
}
const buffer = parquetWriteBuffer({
  columnData: columnData.map(({ name, data }) => ({ name, data })),
  schema, codec: input.codec ?? "SNAPPY",
});
process.stdout.write(Buffer.from(buffer));
