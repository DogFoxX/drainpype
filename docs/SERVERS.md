# Server definitions

DrainPype server definitions are declarative JSON or YAML files. A definition describes a
server's GUI fields, optional API calls made before upload, the HTTP request for each file,
and how to interpret the response. No Python code is needed.

Add a definition from the GUI with **Servers → Add Server**, using a local file or a URL to
a definition. Local definitions are copied into DrainPype's user configuration directory.
Supported file extensions are `.json`, `.yaml`, and `.yml`. YAML requires the
optional PyYAML dependency (`pip install 'drainpype[yaml]'`).

The [JSON Schema](./server.schema.json) documents the supported structure and can be used
with an editor for validation and completion. DrainPype currently supports schema version 1.

## Minimal example

This definition sends each file as a multipart upload and expects the response body to
contain a URL. It illustrates the shape of a definition; check the service's API docs for
its actual endpoint, fields, and response format.

```json
{
  "schema": 1,
  "id": "example_host",
  "name": "Example Host",
  "base_url": "https://example.com",
  "request": {
    "verb": "POST",
    "url": [{"text": "https://example.com/upload"}],
    "body": "multipart",
    "file_field": "file"
  },
  "response": {
    "results": [
      {"at": "data.url", "url": "{value}"}
    ]
  }
}
```

## Definition keys

| Key | Meaning |
|---|---|
| `schema` | Required integer. Currently must be `1`. |
| `id` | Required unique identifier; letters, digits, `_`, and `-` only. |
| `name` | Display name. `title` is accepted as a fallback when `name` is omitted. |
| `base_url` | Optional base used by `{base_url}` and to resolve relative request URLs. |
| `fields` | Optional list of options shown in the GUI. |
| `auth` | Optional OAuth 2.0 configuration. |
| `prepare` | Optional API steps to run before uploading, for example to obtain a folder ID. |
| `capture` | Optional rules that copy values from a successful upload response into later requests. |
| `request` | Required description of the per-file upload request. |
| `response` | Optional response parsing and HTTP error handling rules. |

Field keys must be unique and cannot use reserved template names: `value`, `short_date`,
`base_url`, `filename`, `stem`, `ext`, `mime`, or `size`. See the built-in definitions in
`uploader/assets/default_servers/` for complete working examples.

## Templates and conditions

Text values in URLs, headers, form fields, and bodies can use `{name}` or
`{name|filter|filter}` templates.

- Available values include field keys, values captured by `prepare` or `capture`,
  `base_url`, `short_date` (`YYYY-MM-DD`), and per-file values `filename`, `stem`, `ext`,
  `mime`, and `size` (bytes). In a response result's `url` template, `{value}` is the value
  found at that result rule's `at` path.
- Filters: `trim`, `trim_slash`, `url`, `path`, `b64`, `basic_pass`, `json`, and `gquery`.
- `only_if: [keys]` includes an entry only when every listed value is non-empty.
  `unless: [keys]` skips it when any listed value is non-empty.

`url`, `query`, `headers`, and `form` are arrays of parts, allowing conditional parts and
templates. Their required names are respectively `text`, `param` plus `text`, `header` plus
`text`, and `field` plus `text`.

## GUI fields

Each field needs a unique `key`. Supported `type` values are `text`, `textarea`, `secret`,
`select`, `checkbox`, `notes`, `button`, `urlButton`, and `capture_table`.

Common optional keys include `caption`, `help`, `column`, `mandatory`, `needs`,
`needs_multiple`, `disabled_when_all`, and `checked_when_all`. `secret` fields can save a key.
`select` supports static `choices` and/or remote `choices_from`; a remote source can specify
headers, a timeout, response paths, and fields that trigger a reload. See the schema and
built-in definitions for the type-specific keys and button actions.

## Upload request

```yaml
request:
  verb: POST
  url: [{text: "{base_url}/upload"}]
  query: [{param: folder, text: "{folder_id|url}", only_if: [folder_id]}]
  headers: [{header: Authorization, text: "Bearer {api_key}", only_if: [api_key]}]
  body: multipart          # raw | multipart | multipart_related
  file_field: file         # multipart only
  form: [{field: folderId, text: "{folder_id}", only_if: [folder_id]}]
  metadata: [{text: '{"name": "{filename|json}"}'}]  # multipart_related only
  mime: auto
  keepalive: 30
```

`body` defaults to `raw`: the file is the whole request body. `multipart` sends the file as
a form file part and uses `file_field` for its field name. `multipart_related` sends a
metadata part and a file part, as required by APIs such as Google Drive. `metadata` is used
for `multipart_related`; `form` is used for `multipart`.

`mime` defaults to `auto`. DrainPype detects the type using its built-in extension table,
the system MIME database, and then the file's first bytes. It falls back to
`text/plain` or `application/octet-stream`. The MIME type is sent for the file part, or as
the whole request's `Content-Type` in `raw` mode unless the definition supplies that
header. It is also available as `{mime}`. Set `mime` to a fixed MIME type or template to
override detection. `keepalive` sets curl's keepalive time in seconds; it defaults to 30,
and `0` disables it.

## Prepare and capture

`prepare` is a list of API calls made before uploading. A step can specify `verb`, `url`,
`query`, `headers`, and `json_body`. Use `only_if`, `unless`, or `when_empty` to control
whether the step runs, and `extract` to map response JSON paths to template variables.
Prepared variables are available to following steps and the upload request.

`capture` maps a value captured from a successful upload response to a variable for later
requests. Rules use `from` and `to`, with optional `only_if` and `when_empty` conditions.
This is useful when an API returns an upload token or folder identifier that subsequent
files need.

## Upload response

```yaml
response:
  results:
    - at: data.url
      url: "{value}"
      name_at: data.name
  error_at: [data.message, message]
  error_map: {invalid_api_key: Unauthorized}
  status_errors: {429: Rate Limited}
  upload_error_from: 400
  network_error_from: 500
  fallback_url_pattern: 'https?://[^\s"''<>]+'
```

DrainPype checks `results` in order. A matching `at` JSON path supplies the uploaded
file's URL; `url` formats that value, and `name_at` optionally reads the returned name.
Rules can also check `ok_at`/`ok_equals`, require `starts_with`, and capture additional
values. For plain-text responses, `fallback_url_pattern` can match a URL with a regular
expression.

`error_at` lists JSON paths to search for an error message. `error_map` translates a
returned status or message, and `status_errors` maps HTTP status codes to messages.
By default, HTTP statuses from 400 are treated as upload rejections and statuses from 500
as network/server errors; the thresholds can be changed with `upload_error_from` and
`network_error_from`.
