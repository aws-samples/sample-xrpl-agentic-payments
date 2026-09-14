/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest'
  },
  // TypeScript BEFORE JavaScript. Jest's default order puts 'js' first, so a
  // stale compiled lib/*.js left over from an earlier `tsc` run wins over the
  // .ts source next to it — the suite then passes against code nobody edited.
  // Those .js files are git-ignored build output, so a clean clone never had
  // them and CI never saw this; only a working tree that had run tsc did.
  moduleFileExtensions: ['ts', 'tsx', 'js', 'mjs', 'cjs', 'jsx', 'json', 'node'],
  setupFilesAfterEnv: ['aws-cdk-lib/testhelpers/jest-autoclean'],
};
