// aws-sdk-floci-shim.js
//
// Auto-injected by wizard_server into Node.js Lambda zips that depend on
// aws-sdk v2. AWS SDK v2 does NOT honor the AWS_ENDPOINT_URL env var
// (only v3 does), so without this shim a v2 Lambda running under Floci
// silently hits real AWS, gets InvalidAccessKeyId for our test creds,
// and fails with a confusing error.
//
// This shim is loaded via NODE_OPTIONS=--require=... at Lambda start.
// It only fires if AWS_ENDPOINT_URL is set (so it is a no-op outside
// the local-test environment — defense in depth in case a copy ever
// leaks into a real deploy zip).

if (process.env.AWS_ENDPOINT_URL) {
  try {
    const AWS = require('aws-sdk');
    const endpoint = process.env.AWS_ENDPOINT_URL;

    AWS.config.update({
      endpoint: endpoint,
      s3ForcePathStyle: true,
      sslEnabled: false,
      accessKeyId: process.env.AWS_ACCESS_KEY_ID || 'test',
      secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY || 'test',
      region: process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || 'us-east-1',
    });

    // Patch the constructors of common service classes so clients
    // instantiated with `new AWS.S3()` (no args) also pick up the endpoint.
    // AWS.config.update alone does NOT propagate to already-loaded service
    // classes in older SDK builds — explicit per-client patching is safer.
    const SERVICES = ['S3', 'SQS', 'DynamoDB', 'SNS', 'SES', 'Lambda', 'Kinesis', 'Firehose', 'CloudWatch', 'SecretsManager', 'SSM', 'KMS'];
    for (const svc of SERVICES) {
      if (typeof AWS[svc] !== 'function') continue;
      const Original = AWS[svc];
      AWS[svc] = function (opts = {}) {
        const merged = Object.assign({
          endpoint: endpoint,
          s3ForcePathStyle: svc === 'S3' ? true : undefined,
          sslEnabled: false,
          accessKeyId: process.env.AWS_ACCESS_KEY_ID || 'test',
          secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY || 'test',
          region: process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || 'us-east-1',
        }, opts);
        return new Original(merged);
      };
      // Preserve any static properties / DocumentClient / etc.
      Object.assign(AWS[svc], Original);
      // Mark for diagnostics
      AWS[svc].__floci_patched__ = true;
    }

    console.log(`[floci-shim] AWS SDK v2 redirected to ${endpoint} (services: ${SERVICES.join(',')})`);
  } catch (e) {
    // aws-sdk not installed — Lambda is using v3 or no AWS at all. No-op.
    if (process.env.FLOCI_SHIM_DEBUG === '1') {
      console.log(`[floci-shim] aws-sdk not present, skipping shim: ${e.message}`);
    }
  }
}
