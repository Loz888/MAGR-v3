# 三份完整Draft PR提案审阅

状态：待人工审批，尚未发布业务PR。仅创建下列三个Draft PR，不合并、不上线、不发送Issue评论。

以下是原Action的准确hash及完整payload。审批后仍由原Dispatcher/Outbox核对当前结果、来源、期限与远端完整对象。

冻结manifest：`7b4b7f46c15c0e19d19276a84df4688aeac76f94d3601ba61b97c42f77ce52fd`；模型新增9次，保守US$0.001635；无unknown/预占。

## N01 · p0-commerce-live-v2-n01-base

- Action：`fc9f0463-247e-4ff4-bfa5-46652372d9b3`
- payload_hash：`a7dc957113eee1d3502abb84dc82b435936eb651ddcdefebdfa856bc5b652728`
- 业务结果：passed；证据有效至北京时间 2026-11-06T22:48:07.650451+08:00
- 目标：`loz888/magr-v3`，base `p0-commerce-live-v2-n01-base`，head `mgar/r5-fc9f0463247e4ff4bfa546652372d9b3`

```diff
--- a/v3/examples/repair-target/orders.go
+++ b/v3/examples/repair-target/orders.go
@@ -7,9 +7,13 @@
 }
 
 func PaidRevenueCents(orders []Order) int {
-    total := 0
-    for _, order := range orders {
+total := 0
+seen := make(map[string]bool)
+for _, order := range orders {
+    if order.Status == "paid" && !seen[order.ID] {
+        seen[order.ID] = true
         total += order.Amount
     }
-    return total
 }
+return total
+}
```

完整审批对象：
```json
{
  "type": "draft_pr",
  "target": "github:loz888/magr-v3:draft:p0-commerce-live-v2-n01-base",
  "payload": {
    "body": "Validated bounded repair.\n\n--- a/v3/examples/repair-target/orders.go\n+++ b/v3/examples/repair-target/orders.go\n@@ -7,9 +7,13 @@\n }\n \n func PaidRevenueCents(orders []Order) int {\n-    total := 0\n-    for _, order := range orders {\n+total := 0\n+seen := make(map[string]bool)\n+for _, order := range orders {\n+    if order.Status == \"paid\" && !seen[order.ID] {\n+        seen[order.ID] = true\n         total += order.Amount\n     }\n-    return total\n }\n+return total\n+}\n\n\n<!-- mgar-action:fc9f0463-247e-4ff4-bfa5-46652372d9b3 -->",
    "diff": "--- a/v3/examples/repair-target/orders.go\n+++ b/v3/examples/repair-target/orders.go\n@@ -7,9 +7,13 @@\n }\n \n func PaidRevenueCents(orders []Order) int {\n-    total := 0\n-    for _, order := range orders {\n+total := 0\n+seen := make(map[string]bool)\n+for _, order := range orders {\n+    if order.Status == \"paid\" && !seen[order.ID] {\n+        seen[order.ID] = true\n         total += order.Amount\n     }\n-    return total\n }\n+return total\n+}\n",
    "draft": true,
    "files": [
      {
        "path": "v3/examples/repair-target/orders.go",
        "content": "package orders\n\ntype Order struct {\n    ID string\n    Status string\n    Amount int\n}\n\nfunc PaidRevenueCents(orders []Order) int {\ntotal := 0\nseen := make(map[string]bool)\nfor _, order := range orders {\n    if order.Status == \"paid\" && !seen[order.ID] {\n        seen[order.ID] = true\n        total += order.Amount\n    }\n}\nreturn total\n}\n"
      }
    ],
    "title": "Fix paid order revenue calculation",
    "branch": "mgar/r5-fc9f0463247e4ff4bfa546652372d9b3",
    "base_sha": "6669b7d189aa439bd23334d9b59739f8b48f4406",
    "tree_sha": "101777f8e83c3c2a155f358f7e322c2ec20c68a1",
    "base_tree": "48c657452b8a4a00da8b8ef792b84a8cded1517a",
    "diff_hash": "ef1e56dd498d9aa49b77e1fb4ae0cbf138a49a4afe69b2f64da4f168e0d4fbdd",
    "spec_hash": "d19933afa328d5ea7f833a526ad67f3df2d8965f72984aa0b157987903752212",
    "test_hash": "5e2c1ad1b183691ee5d98247c46aa41d6304797b4a6ab4b3ff6991ab5faf1ebf",
    "repository": "loz888/magr-v3",
    "base_branch": "p0-commerce-live-v2-n01-base",
    "issue_number": 1,
    "source_origin": "github_commit",
    "hidden_receipt": {
      "tests": [
        "TestCommerceObservedWindow",
        "TestPaidRevenueCents",
        "TestR5Hidden",
        "TestR5Hidden/v0",
        "TestR5Hidden/v1",
        "TestR5Hidden/v10",
        "TestR5Hidden/v11",
        "TestR5Hidden/v12",
        "TestR5Hidden/v13",
        "TestR5Hidden/v14",
        "TestR5Hidden/v15",
        "TestR5Hidden/v16",
        "TestR5Hidden/v17",
        "TestR5Hidden/v18",
        "TestR5Hidden/v19",
        "TestR5Hidden/v2",
        "TestR5Hidden/v20",
        "TestR5Hidden/v21",
        "TestR5Hidden/v22",
        "TestR5Hidden/v23",
        "TestR5Hidden/v24",
        "TestR5Hidden/v25",
        "TestR5Hidden/v26",
        "TestR5Hidden/v27",
        "TestR5Hidden/v3",
        "TestR5Hidden/v4",
        "TestR5Hidden/v5",
        "TestR5Hidden/v6",
        "TestR5Hidden/v7",
        "TestR5Hidden/v8",
        "TestR5Hidden/v9"
      ],
      "passed": true,
      "policy": {
        "user": "65532:65532",
        "mounts": [],
        "network": "none",
        "cap_drop": [
          "ALL"
        ],
        "read_only": true,
        "pids_limit": 96,
        "memory_bytes": 536870912,
        "no_new_privileges": true
      },
      "skipped": 0,
      "image_id": "sha256:490fa7f5f4660cb9cf5fb24b9baef1b297065469dec4608888f02b091aea351c",
      "log_hash": "6ab2db7695ab49b96d47313eb52ff4b5e9cdc3f0967b495794aa35a1d7c3507c",
      "exit_code": 0,
      "receipt_id": "9f3cd3da-20fc-4d32-aaff-b2ea3c7a770e",
      "stop_reason": null,
      "failed_tests": [],
      "workspace_hash": "e071c2aa33c313e2619535e1be7bb49a1dbab3bfb351421d2765405ca9efd11e"
    },
    "model_receipts": [
      {
        "call_id": "577794e6-9cf4-49fd-99b4-d4a9be95361c",
        "step_key": "p0-body:r2:a0",
        "profile_hash": "47ed01a10f09642b70c9d0e98b31a0502b9cb78a2321bdc37afd8dfe83ffd433",
        "source_run_id": "671c6c4a-496d-4101-9b7d-dce0f116ee63",
        "transformation": "function_body_v1",
        "function_body_hash": "b65d265343e41998927de76f6584b1e155d357cd5a83ed06fa09fefc63656c00",
        "normalized_diff_hash": "ef1e56dd498d9aa49b77e1fb4ae0cbf138a49a4afe69b2f64da4f168e0d4fbdd"
      }
    ],
    "public_receipt": {
      "tests": [
        "TestPaidRevenueCents"
      ],
      "passed": true,
      "policy": {
        "user": "65532:65532",
        "mounts": [],
        "network": "none",
        "cap_drop": [
          "ALL"
        ],
        "read_only": true,
        "pids_limit": 96,
        "memory_bytes": 536870912,
        "no_new_privileges": true
      },
      "skipped": 0,
      "image_id": "sha256:490fa7f5f4660cb9cf5fb24b9baef1b297065469dec4608888f02b091aea351c",
      "log_hash": "8b5580b83a78da8740b84b1bd61fc0d3713343fa2ac3c18069f07b381a802bb3",
      "exit_code": 0,
      "receipt_id": "35301325-826b-417d-8f78-d985fdd6e702",
      "stop_reason": null,
      "failed_tests": [],
      "workspace_hash": "37d667419ad23e9ecf601bfd545b143f279d83f45ffa09db0cade10b6dfd5bc0"
    },
    "workspace_hash": "0adec3283393116c0551daefcba04d14bde82b371a4b8cb40dc6604b6b9259c8",
    "manual_input_hash": null,
    "business_result_hash": "0a83d6dc12c5f984312890d376f9c1ed4bb2a48465db31793d35e8c6addab128"
  },
  "base_revision": 2
}
```

## N02 · p0-commerce-live-v2-n02-base

- Action：`a0aec30d-e24b-4254-a999-366665581e96`
- payload_hash：`fe27417b1c5165d06055527f0ed9e1eabcf2cc530909b0ba2f503d60159b1171`
- 业务结果：passed；证据有效至北京时间 2026-11-06T22:48:09.984328+08:00
- 目标：`loz888/magr-v3`，base `p0-commerce-live-v2-n02-base`，head `mgar/r5-a0aec30de24b4254a999366665581e96`

```diff
--- a/v3/examples/repair-target/orders.go
+++ b/v3/examples/repair-target/orders.go
@@ -7,9 +7,13 @@
 }
 
 func PaidRevenueCents(orders []Order) int {
-    total := 0
-    for _, order := range orders {
+total := 0
+seen := make(map[string]bool)
+for _, order := range orders {
+    if order.Status == "paid" && !seen[order.ID] {
+        seen[order.ID] = true
         total += order.Amount
     }
-    return total
 }
+return total
+}
```

完整审批对象：
```json
{
  "type": "draft_pr",
  "target": "github:loz888/magr-v3:draft:p0-commerce-live-v2-n02-base",
  "payload": {
    "body": "Validated bounded repair.\n\n--- a/v3/examples/repair-target/orders.go\n+++ b/v3/examples/repair-target/orders.go\n@@ -7,9 +7,13 @@\n }\n \n func PaidRevenueCents(orders []Order) int {\n-    total := 0\n-    for _, order := range orders {\n+total := 0\n+seen := make(map[string]bool)\n+for _, order := range orders {\n+    if order.Status == \"paid\" && !seen[order.ID] {\n+        seen[order.ID] = true\n         total += order.Amount\n     }\n-    return total\n }\n+return total\n+}\n\n\n<!-- mgar-action:a0aec30d-e24b-4254-a999-366665581e96 -->",
    "diff": "--- a/v3/examples/repair-target/orders.go\n+++ b/v3/examples/repair-target/orders.go\n@@ -7,9 +7,13 @@\n }\n \n func PaidRevenueCents(orders []Order) int {\n-    total := 0\n-    for _, order := range orders {\n+total := 0\n+seen := make(map[string]bool)\n+for _, order := range orders {\n+    if order.Status == \"paid\" && !seen[order.ID] {\n+        seen[order.ID] = true\n         total += order.Amount\n     }\n-    return total\n }\n+return total\n+}\n",
    "draft": true,
    "files": [
      {
        "path": "v3/examples/repair-target/orders.go",
        "content": "package orders\n\ntype Order struct {\n    ID string\n    Status string\n    Amount int\n}\n\nfunc PaidRevenueCents(orders []Order) int {\ntotal := 0\nseen := make(map[string]bool)\nfor _, order := range orders {\n    if order.Status == \"paid\" && !seen[order.ID] {\n        seen[order.ID] = true\n        total += order.Amount\n    }\n}\nreturn total\n}\n"
      }
    ],
    "title": "Fix paid order revenue calculation",
    "branch": "mgar/r5-a0aec30de24b4254a999366665581e96",
    "base_sha": "6669b7d189aa439bd23334d9b59739f8b48f4406",
    "tree_sha": "101777f8e83c3c2a155f358f7e322c2ec20c68a1",
    "base_tree": "48c657452b8a4a00da8b8ef792b84a8cded1517a",
    "diff_hash": "ef1e56dd498d9aa49b77e1fb4ae0cbf138a49a4afe69b2f64da4f168e0d4fbdd",
    "spec_hash": "65691ce7deb38f5c34b31902a7dca27231bec7eb8799a5e03656937cc84055a1",
    "test_hash": "5887603273f4327bfe9c50448ad6221fd4287bd38d77c59eaf76260e76d4853a",
    "repository": "loz888/magr-v3",
    "base_branch": "p0-commerce-live-v2-n02-base",
    "issue_number": 1,
    "source_origin": "github_commit",
    "hidden_receipt": {
      "tests": [
        "TestCommerceObservedWindow",
        "TestPaidRevenueCents",
        "TestR5Hidden",
        "TestR5Hidden/v0",
        "TestR5Hidden/v1",
        "TestR5Hidden/v10",
        "TestR5Hidden/v11",
        "TestR5Hidden/v12",
        "TestR5Hidden/v13",
        "TestR5Hidden/v14",
        "TestR5Hidden/v15",
        "TestR5Hidden/v16",
        "TestR5Hidden/v17",
        "TestR5Hidden/v18",
        "TestR5Hidden/v19",
        "TestR5Hidden/v2",
        "TestR5Hidden/v20",
        "TestR5Hidden/v21",
        "TestR5Hidden/v22",
        "TestR5Hidden/v23",
        "TestR5Hidden/v24",
        "TestR5Hidden/v25",
        "TestR5Hidden/v26",
        "TestR5Hidden/v27",
        "TestR5Hidden/v3",
        "TestR5Hidden/v4",
        "TestR5Hidden/v5",
        "TestR5Hidden/v6",
        "TestR5Hidden/v7",
        "TestR5Hidden/v8",
        "TestR5Hidden/v9"
      ],
      "passed": true,
      "policy": {
        "user": "65532:65532",
        "mounts": [],
        "network": "none",
        "cap_drop": [
          "ALL"
        ],
        "read_only": true,
        "pids_limit": 96,
        "memory_bytes": 536870912,
        "no_new_privileges": true
      },
      "skipped": 0,
      "image_id": "sha256:490fa7f5f4660cb9cf5fb24b9baef1b297065469dec4608888f02b091aea351c",
      "log_hash": "18ca89b1f25bf5213df4b06dbce8fec3d837005437d69159d46f58bf3711b615",
      "exit_code": 0,
      "receipt_id": "72536390-c9f4-4025-aece-96fb73e0609c",
      "stop_reason": null,
      "failed_tests": [],
      "workspace_hash": "448c9f937becbd6cbea93dd4002b2bbb331e5e8017bd53717d53af224d4f72ec"
    },
    "model_receipts": [
      {
        "call_id": "1539c03b-c457-4276-9ef8-39c73c81f4f3",
        "step_key": "p0-body:r2:a0",
        "profile_hash": "d5124ec57a7625f16216c5914b6b6c6058968148322cf9b0b257f0b5910325be",
        "source_run_id": "11943f69-7667-4d0f-ae06-e9f08f6746fb",
        "transformation": "function_body_v1",
        "function_body_hash": "b65d265343e41998927de76f6584b1e155d357cd5a83ed06fa09fefc63656c00",
        "normalized_diff_hash": "ef1e56dd498d9aa49b77e1fb4ae0cbf138a49a4afe69b2f64da4f168e0d4fbdd"
      }
    ],
    "public_receipt": {
      "tests": [
        "TestPaidRevenueCents"
      ],
      "passed": true,
      "policy": {
        "user": "65532:65532",
        "mounts": [],
        "network": "none",
        "cap_drop": [
          "ALL"
        ],
        "read_only": true,
        "pids_limit": 96,
        "memory_bytes": 536870912,
        "no_new_privileges": true
      },
      "skipped": 0,
      "image_id": "sha256:490fa7f5f4660cb9cf5fb24b9baef1b297065469dec4608888f02b091aea351c",
      "log_hash": "9d2b865a0bebc7368b51681c64b2c0c6b7cc56a4f2ba5846011dc9a699d1ffbd",
      "exit_code": 0,
      "receipt_id": "a211ccae-447c-4cab-becc-3d36dadae4a3",
      "stop_reason": null,
      "failed_tests": [],
      "workspace_hash": "37d667419ad23e9ecf601bfd545b143f279d83f45ffa09db0cade10b6dfd5bc0"
    },
    "workspace_hash": "0adec3283393116c0551daefcba04d14bde82b371a4b8cb40dc6604b6b9259c8",
    "manual_input_hash": null,
    "business_result_hash": "2a07dacc793ba8e6f410c26b6c8f84eb7cc01a440c473fa5e7ab9e119483b35f"
  },
  "base_revision": 2
}
```

## N03 · p0-commerce-live-v2-n03-base

- Action：`a32f6b32-eb08-461e-9fb1-7816d54b3709`
- payload_hash：`f93483cd1f77081650dd13bfd34416a06f8faede0e130715b0e9f52af9f88d97`
- 业务结果：passed；证据有效至北京时间 2026-11-06T22:48:12.120795+08:00
- 目标：`loz888/magr-v3`，base `p0-commerce-live-v2-n03-base`，head `mgar/r5-a32f6b32eb08461e9fb17816d54b3709`

```diff
--- a/v3/examples/repair-target/orders.go
+++ b/v3/examples/repair-target/orders.go
@@ -7,9 +7,13 @@
 }
 
 func PaidRevenueCents(orders []Order) int {
-    total := 0
-    for _, order := range orders {
+total := 0
+seen := make(map[string]bool)
+for _, order := range orders {
+    if order.Status == "paid" && !seen[order.ID] {
+        seen[order.ID] = true
         total += order.Amount
     }
-    return total
 }
+return total
+}
```

完整审批对象：
```json
{
  "type": "draft_pr",
  "target": "github:loz888/magr-v3:draft:p0-commerce-live-v2-n03-base",
  "payload": {
    "body": "Validated bounded repair.\n\n--- a/v3/examples/repair-target/orders.go\n+++ b/v3/examples/repair-target/orders.go\n@@ -7,9 +7,13 @@\n }\n \n func PaidRevenueCents(orders []Order) int {\n-    total := 0\n-    for _, order := range orders {\n+total := 0\n+seen := make(map[string]bool)\n+for _, order := range orders {\n+    if order.Status == \"paid\" && !seen[order.ID] {\n+        seen[order.ID] = true\n         total += order.Amount\n     }\n-    return total\n }\n+return total\n+}\n\n\n<!-- mgar-action:a32f6b32-eb08-461e-9fb1-7816d54b3709 -->",
    "diff": "--- a/v3/examples/repair-target/orders.go\n+++ b/v3/examples/repair-target/orders.go\n@@ -7,9 +7,13 @@\n }\n \n func PaidRevenueCents(orders []Order) int {\n-    total := 0\n-    for _, order := range orders {\n+total := 0\n+seen := make(map[string]bool)\n+for _, order := range orders {\n+    if order.Status == \"paid\" && !seen[order.ID] {\n+        seen[order.ID] = true\n         total += order.Amount\n     }\n-    return total\n }\n+return total\n+}\n",
    "draft": true,
    "files": [
      {
        "path": "v3/examples/repair-target/orders.go",
        "content": "package orders\n\ntype Order struct {\n    ID string\n    Status string\n    Amount int\n}\n\nfunc PaidRevenueCents(orders []Order) int {\ntotal := 0\nseen := make(map[string]bool)\nfor _, order := range orders {\n    if order.Status == \"paid\" && !seen[order.ID] {\n        seen[order.ID] = true\n        total += order.Amount\n    }\n}\nreturn total\n}\n"
      }
    ],
    "title": "Fix paid order revenue calculation",
    "branch": "mgar/r5-a32f6b32eb08461e9fb17816d54b3709",
    "base_sha": "6669b7d189aa439bd23334d9b59739f8b48f4406",
    "tree_sha": "101777f8e83c3c2a155f358f7e322c2ec20c68a1",
    "base_tree": "48c657452b8a4a00da8b8ef792b84a8cded1517a",
    "diff_hash": "ef1e56dd498d9aa49b77e1fb4ae0cbf138a49a4afe69b2f64da4f168e0d4fbdd",
    "spec_hash": "3103a85093fd25a5c871021a2582d6d2577c5ae9b5ff5d1efdf61d26ea839bd5",
    "test_hash": "fa6fe34cb33cc1bfc46afad98b97a9ad1b3e5a78f017724013c37bd957ec5625",
    "repository": "loz888/magr-v3",
    "base_branch": "p0-commerce-live-v2-n03-base",
    "issue_number": 1,
    "source_origin": "github_commit",
    "hidden_receipt": {
      "tests": [
        "TestCommerceObservedWindow",
        "TestPaidRevenueCents",
        "TestR5Hidden",
        "TestR5Hidden/v0",
        "TestR5Hidden/v1",
        "TestR5Hidden/v10",
        "TestR5Hidden/v11",
        "TestR5Hidden/v12",
        "TestR5Hidden/v13",
        "TestR5Hidden/v14",
        "TestR5Hidden/v15",
        "TestR5Hidden/v16",
        "TestR5Hidden/v17",
        "TestR5Hidden/v18",
        "TestR5Hidden/v19",
        "TestR5Hidden/v2",
        "TestR5Hidden/v20",
        "TestR5Hidden/v21",
        "TestR5Hidden/v22",
        "TestR5Hidden/v23",
        "TestR5Hidden/v24",
        "TestR5Hidden/v25",
        "TestR5Hidden/v26",
        "TestR5Hidden/v27",
        "TestR5Hidden/v3",
        "TestR5Hidden/v4",
        "TestR5Hidden/v5",
        "TestR5Hidden/v6",
        "TestR5Hidden/v7",
        "TestR5Hidden/v8",
        "TestR5Hidden/v9"
      ],
      "passed": true,
      "policy": {
        "user": "65532:65532",
        "mounts": [],
        "network": "none",
        "cap_drop": [
          "ALL"
        ],
        "read_only": true,
        "pids_limit": 96,
        "memory_bytes": 536870912,
        "no_new_privileges": true
      },
      "skipped": 0,
      "image_id": "sha256:490fa7f5f4660cb9cf5fb24b9baef1b297065469dec4608888f02b091aea351c",
      "log_hash": "ffa9e16e300fb9018e33858b9813de1f1c76ee32008d663c10c5242be3445bff",
      "exit_code": 0,
      "receipt_id": "120b508e-6fd2-4535-b34c-45ec830dee46",
      "stop_reason": null,
      "failed_tests": [],
      "workspace_hash": "a69ec3f0e0a1c02c16a22301e4ede55811e45fe7f4072438f5de107c19d49e36"
    },
    "model_receipts": [
      {
        "call_id": "8e54be16-d222-445c-826c-7ec648fd662e",
        "step_key": "p0-body:r2:a0",
        "profile_hash": "89d551a6572f615d90e294c89634b4d9a9729cfbe23f4b53aca64d5bf6a78952",
        "source_run_id": "01b3b01f-ea76-41e2-a2ba-64694a73fc43",
        "transformation": "function_body_v1",
        "function_body_hash": "b65d265343e41998927de76f6584b1e155d357cd5a83ed06fa09fefc63656c00",
        "normalized_diff_hash": "ef1e56dd498d9aa49b77e1fb4ae0cbf138a49a4afe69b2f64da4f168e0d4fbdd"
      }
    ],
    "public_receipt": {
      "tests": [
        "TestPaidRevenueCents"
      ],
      "passed": true,
      "policy": {
        "user": "65532:65532",
        "mounts": [],
        "network": "none",
        "cap_drop": [
          "ALL"
        ],
        "read_only": true,
        "pids_limit": 96,
        "memory_bytes": 536870912,
        "no_new_privileges": true
      },
      "skipped": 0,
      "image_id": "sha256:490fa7f5f4660cb9cf5fb24b9baef1b297065469dec4608888f02b091aea351c",
      "log_hash": "5a9757eb5089e5448127d19f9cdecaf44666d5d2f1fb94dc0d22a26e9c8a2754",
      "exit_code": 0,
      "receipt_id": "d215df8b-1b29-409c-8402-f9558a7f38c8",
      "stop_reason": null,
      "failed_tests": [],
      "workspace_hash": "37d667419ad23e9ecf601bfd545b143f279d83f45ffa09db0cade10b6dfd5bc0"
    },
    "workspace_hash": "0adec3283393116c0551daefcba04d14bde82b371a4b8cb40dc6604b6b9259c8",
    "manual_input_hash": null,
    "business_result_hash": "ce8575286bc10e489dc138d59390e2aa4f582559eede4d62cffcbb9ce4411b12"
  },
  "base_revision": 2
}
```
