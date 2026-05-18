# Partition template

This is the skeleton used by `st-setup new-partition` to scaffold a new
Guardian partition. Files use placeholders that the script substitutes:

- `__PARTITION__` — partition name (also used as namespace and asset prefix).
- `__VERSION__`   — initial asset version stamp (defaults to `YYYYMMDD-HHMM`).

Usage:

   st-setup new-partition my-thing

After scaffolding:

1. Edit `partitions/my-thing/intents/my-thing.yaml` to set the real
   image, env, ports, volume size and dependencies.
2. Add the partition to `st-image` `partition_image_specs()` and
   `supported_partitions()` so `st-image build|push|stamp` knows about it.
3. Build & roll out:

      st-release --partition my-thing
