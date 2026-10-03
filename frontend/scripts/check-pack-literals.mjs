// 화면은 Pack을 모른다: frontend/src의 따옴표 문자열에 Pack ID가 나오면 실패한다.
// domain_packs/*/ YAML에서 zone·resource·pool·task·work_type·자원 속성·풀 종류·rule_id·site_id·timezone ID를 읽는다.
// 한두 글자 ID는 오탐이 많아 길이 3 이상만 검사한다. npm 라이브러리 없이 필요한 키만 정규식으로 읽는다.
//   node scripts/check-pack-literals.mjs   (npm run lint에 포함)

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontend = join(here, '..')
const packsDir = join(frontend, '..', 'domain_packs')
const srcDir = join(frontend, 'src')
const MIN_LEN = 3

const clean = (v) =>
  v
    .replace(/\s+#.*$/, '')
    .trim()
    .replace(/[,}\]]+$/, '')
    .trim()
    .replace(/^['"]|['"]$/g, '')

/** 한 Pack의 ID → 종류. */
function packIds(dir) {
  const ids = new Map()
  const add = (id, kind) => {
    if (id && id.length >= MIN_LEN && id !== 'null') ids.set(id, kind)
  }
  for (const f of readdirSync(dir).filter((n) => n.endsWith('.yaml'))) {
    const text = readFileSync(join(dir, f), 'utf8')
    for (const [key, kind] of [
      ['site_id', 'site_id'],
      ['timezone', 'timezone'],
      ['resource_id', 'resource'],
      ['pool_id', 'pool'],
      ['task_id', 'task'],
      ['rule_id', 'rule_id'],
    ]) {
      for (const m of text.matchAll(new RegExp(`(?:^|[\\s{,])${key}:\\s*([^,}\\n]+)`, 'gm'))) {
        add(clean(m[1]), kind)
      }
    }
    // zones: [a, b] 또는 블록 목록
    const flow = /^zones:\s*\[([^\]]*)\]/m.exec(text)
    if (flow) flow[1].split(',').forEach((z) => add(clean(z), 'zone'))
    const block = /^zones:\s*\n((?:\s+-\s+.*\n?)+)/m.exec(text)
    if (block) block[1].split('\n').forEach((l) => add(clean(l.replace(/^\s+-\s+/, '')), 'zone'))
    // work_types: 아래 2칸 들여쓴 키
    const wt = /^work_types:\s*\n((?:(?:\s{2}.*)?\n?)+)/m.exec(text)
    if (wt) {
      for (const m of wt[1].matchAll(/^ {2}([A-Za-z_][\w-]*)\s*:/gm)) add(m[1], 'work_type')
    }
    // resource_attributes·pool_kinds: 아래 2칸 들여쓴 키 (자원 속성 이름, 수량 풀 종류)
    for (const [section, kind] of [
      ['resource_attributes', 'resource_attribute'],
      ['pool_kinds', 'pool_kind'],
    ]) {
      const block = new RegExp(`^${section}:\\s*\\n((?:(?:\\s{2}.*)?\\n?)+)`, 'm').exec(text)
      if (block) {
        for (const m of block[1].matchAll(/^ {2}([A-Za-z_][\w-]*)\s*:/gm)) add(m[1], kind)
      }
    }
  }
  return ids
}

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) return walk(p)
    return /\.(ts|tsx)$/.test(n) ? [p] : []
  })
}

const ids = new Map()
for (const n of readdirSync(packsDir)) {
  const d = join(packsDir, n)
  if (statSync(d).isDirectory()) for (const [id, kind] of packIds(d)) ids.set(id, kind)
}
if (ids.size === 0) {
  console.error('check-pack-literals: Pack ID를 읽지 못했다 (domain_packs 확인)')
  process.exit(2)
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const patterns = [...ids].map(([id, kind]) => ({
  id,
  kind,
  re: new RegExp(`(^|[^A-Za-z0-9_-])${escape(id)}($|[^A-Za-z0-9_-])`),
}))
const literal = /'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`/g

const found = []
for (const file of walk(srcDir)) {
  const text = readFileSync(file, 'utf8')
  for (const m of text.matchAll(literal)) {
    const body = m[0].slice(1, -1)
    for (const p of patterns) {
      if (p.re.test(body)) {
        const line = text.slice(0, m.index).split('\n').length
        found.push(`${relative(frontend, file)}:${line}: ${m[0]} — Pack ${p.kind} "${p.id}"`)
      }
    }
  }
}

if (found.length) {
  console.error('화면 코드에 Pack 값이 있다 (meta·scenario·state에서 받아야 한다):')
  for (const f of found) console.error(`  ${f}`)
  process.exit(1)
}
console.log(`check-pack-literals: OK (Pack ID ${ids.size}개, 길이 ${MIN_LEN} 이상)`)
