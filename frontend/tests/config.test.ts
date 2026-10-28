import assert from 'node:assert/strict'
import test from 'node:test'

import { DEFAULT_ERP_DELIVERY_URL } from '../src/config.ts'

test('uses the official ERP receive endpoint as the editable UI default', () => {
  assert.equal(
    DEFAULT_ERP_DELIVERY_URL,
    'https://qingflow.com/api/qsource/eab3897c-4748-47b5-9121-96683b36cf0c',
  )
})
