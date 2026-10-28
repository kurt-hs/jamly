"use strict";

var assert = require("assert");
var fs = require("fs");

var intervalCallback = null;
var stored = null;

global.importScripts = function () {};
global.setInterval = function (callback) {
  intervalCallback = callback;
  return 1;
};
global.autoConvertClient = {
  pollStatus: function () {
    return Promise.resolve({ pending: 1, done: 2 });
  },
  addInputs: function () {
    return Promise.resolve({ newly_added: 1 });
  },
};
global.chrome = {
  runtime: {
    getURL: function (path) { return path; },
    sendMessage: function () {},
    onMessage: { addListener: function () {} },
  },
  storage: {
    local: {
      get: function (_keys, callback) { callback({}); },
      set: function (value) { stored = value; },
    },
    onChanged: { addListener: function () {} },
  },
};

eval(fs.readFileSync(__dirname + "/jamly_extension_bg.js", "utf8"));
assert.ok(intervalCallback, "background script registered a poll");

intervalCallback();
setTimeout(function () {
  assert.deepStrictEqual(stored.ac_status, { pending: 1, done: 2 });
  assert.strictEqual(
    stored.ac_status instanceof Promise,
    false,
    "resolved status, not a Promise, must be stored"
  );
  process.stdout.write("background async status storage: OK\n");
}, 0);
