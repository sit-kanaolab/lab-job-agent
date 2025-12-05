# Supabase マイグレーション管理

## 運用ルール
- 変更は `supabase/migrations/` に連番SQLを追加する（例: `20251205180335_init_schema.sql`, `20251206120000_add_xxx.sql`）。
- GUIで直接スキーマをいじらず、SQLファイルをSQL Editorに貼って適用する。
- supabase CLI を使う場合も、このディレクトリをソースオブトゥルースとする。

## 適用状況メモ
- 本番 Supabase: `20251205180335_init_schema.sql` まで適用済み（更新したらここを書き換える）

## 手動適用手順
1. Supabase コンソール → SQL Editor を開く。
2. 未適用のファイルから順に内容を貼り付けて実行する。
3. 終わったら上の「適用状況メモ」を更新する。

## 20251205180335_init_schema.sql の概要
- tables: `users`, `scripts`, `jobs`, `jupyter_sessions`
- 拡張: `pgcrypto`
- 制約: status の CHECK、`scripts` の unique(user_id,path)、`jobs` の script_id/script_path いずれか必須
- インデックス: `scripts(user_id)`, `jobs(user_id,status,created_at)`, `jupyter_sessions(user_id,status,created_at)`
- RLS: 全テーブルで `user_id = auth.uid()` の行のみ操作可能（service key は無視）
- トリガ: `jupyter_sessions.updated_at` を更新時に now() へ自動設定
