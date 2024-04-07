#!/usr/bin/env bash

set -eu

url="https://raw.githubusercontent.com/compose-spec/compose-spec/master/schema/compose-spec.json"

datamodel-codegen --url "$url" --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.10 --disable-timestamp --input-file-type jsonschema \
  --reuse-model --use-union-operator --output scripts/dc.py