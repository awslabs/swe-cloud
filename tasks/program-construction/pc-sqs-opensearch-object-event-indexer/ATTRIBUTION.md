# Attribution

Upstream: https://github.com/vectordotdev/vector
Revision: 0d0b35865036f8b5c2f0104d9744fa84985ffff1

Licenses and notices for distribution only; not installed in task containers.

## Corresponding source

The reference program in this task (environment/reference/program and its copy solution/reference/program) is an unmodified build of Vector 0.60.0, licensed MPL-2.0 (below). No upstream file was changed; the build only selects a reduced feature set (the aws_s3 source and the elasticsearch sink) and release-profile settings on the command line. Its complete corresponding source is the upstream repository https://github.com/vectordotdev/vector at commit 0d0b35865036f8b5c2f0104d9744fa84985ffff1, with the dependencies its lock files pin (Cargo.lock). It was built with Rust 1.96.1 and musl-gcc, after `cargo +1.96.1 fetch --locked`, from the source root at /source:

```sh
CARGO_NET_OFFLINE=true CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
CARGO_PROFILE_RELEASE_DEBUG=false CARGO_PROFILE_RELEASE_STRIP=symbols CARGO_PROFILE_RELEASE_INCREMENTAL=false \
CC_x86_64_unknown_linux_musl=musl-gcc CFLAGS_x86_64_unknown_linux_musl=-I/opt/rust/musl-kheaders \
RUSTFLAGS="--remap-path-prefix=/source=/vector --remap-path-prefix=/opt/rust/cargo=/cargo --remap-path-prefix=/tmp/vector-target=/target" \
cargo +1.96.1 build --locked --release --target x86_64-unknown-linux-musl \
  --no-default-features --features sources-aws_s3,sinks-elasticsearch \
  --target-dir /tmp/vector-target --bin vector
install -m 0755 /tmp/vector-target/x86_64-unknown-linux-musl/release/vector program
```

/opt/rust/musl-kheaders holds copies of the Linux kernel UAPI headers (linux/, asm/, asm-generic/) for the musl C toolchain. The shipped binary has SHA-256 ea77007b6887a64f1d6541030c83db1460450891940922f592cee967da94f786.

## legal/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/LICENSE-3rdparty.csv

```text
Component,Origin,License,Copyright
adler2,https://github.com/oyvindln/adler2,0BSD OR MIT OR Apache-2.0,"Jonas Schievink <jonasschievink@gmail.com>, oyvindln <oyvindln@users.noreply.github.com>"
aead,https://github.com/RustCrypto/traits,MIT OR Apache-2.0,RustCrypto Developers
aes,https://github.com/RustCrypto/block-ciphers,MIT OR Apache-2.0,RustCrypto Developers
aes-siv,https://github.com/RustCrypto/AEADs,Apache-2.0 OR MIT,RustCrypto Developers
ahash,https://github.com/tkaitchuck/ahash,MIT OR Apache-2.0,Tom Kaitchuck <Tom.Kaitchuck@gmail.com>
aho-corasick,https://github.com/BurntSushi/aho-corasick,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
aliasable,https://github.com/avitex/rust-aliasable,MIT,avitex <avitex@wfxlabs.com>
alloc-no-stdlib,https://github.com/dropbox/rust-alloc-no-stdlib,BSD-3-Clause,Daniel Reiter Horn <danielrh@dropbox.com>
alloc-stdlib,https://github.com/dropbox/rust-alloc-no-stdlib,BSD-3-Clause,Daniel Reiter Horn <danielrh@dropbox.com>
allocator-api2,https://github.com/zakarumych/allocator-api2,MIT OR Apache-2.0,Zakarum <zaq.dev@icloud.com>
amq-protocol,https://github.com/amqp-rs/amq-protocol,BSD-2-Clause,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
amq-protocol-tcp,https://github.com/amqp-rs/amq-protocol,BSD-2-Clause,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
amq-protocol-types,https://github.com/amqp-rs/amq-protocol,BSD-2-Clause,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
amq-protocol-uri,https://github.com/amqp-rs/amq-protocol,BSD-2-Clause,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
android_system_properties,https://github.com/nical/android_system_properties,MIT OR Apache-2.0,Nicolas Silva <nical@fastmail.com>
annotate-snippets,https://github.com/rust-lang/annotate-snippets-rs,MIT OR Apache-2.0,The annotate-snippets Authors
anstream,https://github.com/rust-cli/anstyle,MIT OR Apache-2.0,The anstream Authors
anstyle,https://github.com/rust-cli/anstyle,MIT OR Apache-2.0,The anstyle Authors
anstyle-parse,https://github.com/rust-cli/anstyle,MIT OR Apache-2.0,The anstyle-parse Authors
anstyle-query,https://github.com/rust-cli/anstyle,MIT OR Apache-2.0,The anstyle-query Authors
anstyle-wincon,https://github.com/rust-cli/anstyle,MIT OR Apache-2.0,The anstyle-wincon Authors
antithesis-instrumentation,https://github.com/antithesishq/antithesis-instrumentation-rust,MIT,The antithesis-instrumentation Authors
antithesis_sdk,https://github.com/antithesishq/antithesis-sdk-rust,MIT,The antithesis_sdk Authors
anyhow,https://github.com/dtolnay/anyhow,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
apache-avro,https://github.com/apache/avro-rs,Apache-2.0,The apache-avro Authors
approx,https://github.com/brendanzab/approx,Apache-2.0,Brendan Zabarauskas <bjzaba@yahoo.com.au>
arbitrary,https://github.com/rust-fuzz/arbitrary,MIT OR Apache-2.0,"The Rust-Fuzz Project Developers, Nick Fitzgerald <fitzgen@gmail.com>, Manish Goregaokar <manishsmail@gmail.com>, Simonas Kazlauskas <arbitrary@kazlauskas.me>, Brian L. Troutwine <brian@troutwine.us>, Corey Farwell <coreyf@rwell.org>"
arc-swap,https://github.com/vorner/arc-swap,MIT OR Apache-2.0,Michal 'vorner' Vaner <vorner@vorner.cz>
arr_macro,https://github.com/JoshMcguigan/arr_macro,MIT OR Apache-2.0,Josh Mcguigan
arr_macro_impl,https://github.com/JoshMcguigan/arr_macro,MIT OR Apache-2.0,Josh Mcguigan
arraydeque,https://github.com/andylokandy/arraydeque,MIT OR Apache-2.0,andylokandy
arrayvec,https://github.com/bluss/arrayvec,MIT OR Apache-2.0,bluss
arrow,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-arith,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-array,https://github.com/apache/arrow-rs,Apache-2.0 AND MIT,Apache Arrow <dev@arrow.apache.org>
arrow-buffer,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-cast,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-csv,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-data,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-flight,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-ipc,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-json,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-ord,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-row,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-schema,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-select,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
arrow-string,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
async-broadcast,https://github.com/smol-rs/async-broadcast,MIT OR Apache-2.0,"Stjepan Glavina <stjepang@gmail.com>, Yoshua Wuyts <yoshuawuyts@gmail.com>, Zeeshan Ali Khan <zeeshanak@gnome.org>"
async-channel,https://github.com/smol-rs/async-channel,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-compat,https://github.com/smol-rs/async-compat,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-compression,https://github.com/Nullus157/async-compression,MIT OR Apache-2.0,"Wim Looman <wim@nemo157.com>, Allen Bui <fairingrey@gmail.com>"
async-executor,https://github.com/smol-rs/async-executor,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, John Nunley <dev@notgull.net>"
async-fs,https://github.com/smol-rs/async-fs,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-global-executor,https://github.com/Keruspe/async-global-executor,Apache-2.0 OR MIT,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
async-io,https://github.com/smol-rs/async-io,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-lock,https://github.com/smol-rs/async-lock,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-native-tls,https://github.com/async-email/async-native-tls,MIT OR Apache-2.0,dignifiedquire <me@dignifiedquire.com>
async-nats,https://github.com/nats-io/nats.rs,Apache-2.0,"Tomasz Pietrek <tomasz@nats.io>, Casper Beyer <caspervonb@pm.me>"
async-net,https://github.com/smol-rs/async-net,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-process,https://github.com/smol-rs/async-process,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-recursion,https://github.com/dcchut/async-recursion,MIT OR Apache-2.0,Robert Usher <266585+dcchut@users.noreply.github.com>
async-rs,https://github.com/amqp-rs/async-rs,BSD-2-Clause,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
async-signal,https://github.com/smol-rs/async-signal,Apache-2.0 OR MIT,John Nunley <dev@notgull.net>
async-stream,https://github.com/tokio-rs/async-stream,MIT,Carl Lerche <me@carllerche.com>
async-stream-impl,https://github.com/tokio-rs/async-stream,MIT,Carl Lerche <me@carllerche.com>
async-task,https://github.com/smol-rs/async-task,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
async-trait,https://github.com/dtolnay/async-trait,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
asyncband,https://github.com/fast/asyncband,Apache-2.0,The asyncband Authors
atoi,https://github.com/pacman82/atoi-rs,MIT,Markus Klein
atomic-waker,https://github.com/smol-rs/atomic-waker,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, Contributors to futures-rs"
aws-config,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-credential-types,https://github.com/smithy-lang/smithy-rs,Apache-2.0,AWS Rust SDK Team <aws-sdk-rust@amazon.com>
aws-runtime,https://github.com/smithy-lang/smithy-rs,Apache-2.0,AWS Rust SDK Team <aws-sdk-rust@amazon.com>
aws-sdk-cloudwatch,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-cloudwatchlogs,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-elasticsearch,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-firehose,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-kinesis,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-kms,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-s3,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-secretsmanager,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-sns,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-sqs,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-sso,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-ssooidc,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sdk-sts,https://github.com/awslabs/aws-sdk-rust,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-sigv4,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, David Barsky <me@davidbarsky.com>"
aws-smithy-async,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, John DiSanti <jdisanti@amazon.com>"
aws-smithy-cbor,https://github.com/awslabs/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, David Pérez <d@vidp.dev>"
aws-smithy-checksums,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Zelda Hessler <zhessler@amazon.com>"
aws-smithy-compression,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Zelda Hessler <zhessler@amazon.com>"
aws-smithy-eventstream,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, John DiSanti <jdisanti@amazon.com>"
aws-smithy-http,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-smithy-http-client,https://github.com/smithy-lang/smithy-rs,Apache-2.0,AWS Rust SDK Team <aws-sdk-rust@amazon.com>
aws-smithy-json,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, John DiSanti <jdisanti@amazon.com>"
aws-smithy-observability,https://github.com/awslabs/smithy-rs,Apache-2.0,AWS Rust SDK Team <aws-sdk-rust@amazon.com>
aws-smithy-query,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, John DiSanti <jdisanti@amazon.com>"
aws-smithy-runtime,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Zelda Hessler <zhessler@amazon.com>"
aws-smithy-runtime-api,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Zelda Hessler <zhessler@amazon.com>"
aws-smithy-runtime-api-macros,https://github.com/smithy-lang/smithy-rs,Apache-2.0,AWS Rust SDK Team <aws-sdk-rust@amazon.com>
aws-smithy-schema,https://github.com/smithy-lang/smithy-rs,Apache-2.0,AWS Rust SDK Team <aws-sdk-rust@amazon.com>
aws-smithy-types,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-smithy-xml,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
aws-types,https://github.com/smithy-lang/smithy-rs,Apache-2.0,"AWS Rust SDK Team <aws-sdk-rust@amazon.com>, Russell Cohen <rcoh@amazon.com>"
axum,https://github.com/tokio-rs/axum,MIT,The axum Authors
axum-core,https://github.com/tokio-rs/axum,MIT,The axum-core Authors
azure_core,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
azure_core_macros,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
azure_identity,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
azure_storage_blob,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
backon,https://github.com/Xuanwo/backon,Apache-2.0,The backon Authors
base16,https://github.com/thomcc/rust-base16,CC0-1.0,Thom Chiovoloni <tchiovoloni@mozilla.com>
base16ct,https://github.com/RustCrypto/formats/tree/master/base16ct,Apache-2.0 OR MIT,RustCrypto Developers
base62,https://github.com/fbernier/base62,MIT,"François Bernier <frankbernier@gmail.com>, Chai T. Rex <ChaiTRex@users.noreply.github.com>"
base64,https://github.com/marshallpierce/rust-base64,MIT OR Apache-2.0,"Alice Maz <alice@alicemaz.com>, Marshall Pierce <marshall@mpierce.org>"
base64,https://github.com/marshallpierce/rust-base64,MIT OR Apache-2.0,Marshall Pierce <marshall@mpierce.org>
base64-simd,https://github.com/Nugine/simd,MIT,The base64-simd Authors
base64ct,https://github.com/RustCrypto/formats/tree/master/base64ct,Apache-2.0 OR MIT,RustCrypto Developers
bigdecimal,https://github.com/akubera/bigdecimal-rs,MIT OR Apache-2.0,Andrew Kubera
bit-set,https://github.com/contain-rs/bit-set,Apache-2.0 OR MIT,Alexis Beingessner <a.beingessner@gmail.com>
bit-vec,https://github.com/contain-rs/bit-vec,Apache-2.0 OR MIT,Alexis Beingessner <a.beingessner@gmail.com>
bitflags,https://github.com/bitflags/bitflags,MIT OR Apache-2.0,The Rust Project Developers
bitmask-enum,https://github.com/Lukas3674/rust-bitmask-enum,MIT OR Apache-2.0,Lukas3674 <lukashassler@web.de>
bitvec,https://github.com/bitvecto-rs/bitvec,MIT,The bitvec Authors
block-buffer,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
block-padding,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
blocking,https://github.com/smol-rs/blocking,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
bloomy,https://docs.rs/bloomy/,MIT,"Aleksandr Bezobchuk <aleks.bezobchuk@gmail.com>, Alexis Sellier <self@cloudhead.io>"
bollard,https://github.com/fussybeaver/bollard,Apache-2.0,Bollard contributors
bollard-stubs,https://github.com/fussybeaver/bollard,Apache-2.0,Bollard contributors
bon,https://github.com/elastio/bon,MIT OR Apache-2.0,The bon Authors
bon-macros,https://github.com/elastio/bon,MIT OR Apache-2.0,The bon-macros Authors
borrow-or-share,https://github.com/yescallop/borrow-or-share,MIT-0,Scallop Ye <yescallop@gmail.com>
brotli,https://github.com/dropbox/rust-brotli,BSD-3-Clause AND MIT,"Daniel Reiter Horn <danielrh@dropbox.com>, The Brotli Authors"
brotli-decompressor,https://github.com/dropbox/rust-brotli-decompressor,BSD-3-Clause OR MIT,"Daniel Reiter Horn <danielrh@dropbox.com>, The Brotli Authors"
bs58,https://github.com/Nullus157/bs58-rs,MIT OR Apache-2.0,The bs58 Authors
bson,https://github.com/mongodb/bson-rust,MIT,"Y. T. Chung <zonyitoo@gmail.com>, Kevin Yeh <kevinyeah@utexas.edu>, Saghm Rossi <saghmrossi@gmail.com>, Patrick Freed <patrick.freed@mongodb.com>, Isabel Atkinson <isabel.atkinson@mongodb.com>, Abraham Egnor <abraham.egnor@mongodb.com>"
bstr,https://github.com/BurntSushi/bstr,MIT OR Apache-2.0,Andrew Gallant <jamslam@gmail.com>
bumpalo,https://github.com/fitzgen/bumpalo,MIT OR Apache-2.0,Nick Fitzgerald <fitzgen@gmail.com>
by_address,https://github.com/mbrubeck/by_address,MIT OR Apache-2.0,Matt Brubeck <mbrubeck@limpet.net>
bytecheck,https://github.com/djkoloski/bytecheck,MIT,David Koloski <djkoloski@gmail.com>
bytecheck_derive,https://github.com/djkoloski/bytecheck,MIT,David Koloski <djkoloski@gmail.com>
bytecount,https://github.com/llogiq/bytecount,Apache-2.0 OR MIT,"Andre Bogus <bogusandre@gmail.de>, Joshua Landau <joshua@landau.ws>"
bytemuck,https://github.com/Lokathor/bytemuck,Zlib OR Apache-2.0 OR MIT,Lokathor <zefria@gmail.com>
byteorder,https://github.com/BurntSushi/byteorder,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
bytes,https://github.com/tokio-rs/bytes,MIT,"Carl Lerche <me@carllerche.com>, Sean McArthur <sean@seanmonstar.com>"
bytes-utils,https://github.com/vorner/bytes-utils,Apache-2.0 OR MIT,Michal 'vorner' Vaner <vorner@vorner.cz>
bytesize,https://github.com/bytesize-rs/bytesize,Apache-2.0,"Hyunsik Choi <hyunsik.choi@gmail.com>, MrCroxx <mrcroxx@outlook.com>, Rob Ede <robjtede@icloud.com>"
bytestring,https://github.com/actix/actix-net,MIT OR Apache-2.0,"Nikolay Kim <fafhrd91@gmail.com>, Rob Ede <robjtede@icloud.com>"
castaway,https://github.com/sagebind/castaway,MIT,Stephen M. Coakley <me@stephencoakley.com>
cbc,https://github.com/RustCrypto/block-modes,MIT OR Apache-2.0,RustCrypto Developers
cfb-mode,https://github.com/RustCrypto/block-modes,MIT OR Apache-2.0,RustCrypto Developers
cfg-if,https://github.com/rust-lang/cfg-if,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
chacha20,https://github.com/RustCrypto/stream-ciphers,MIT OR Apache-2.0,RustCrypto Developers
chacha20poly1305,https://github.com/RustCrypto/AEADs,Apache-2.0 OR MIT,RustCrypto Developers
charset,https://github.com/hsivonen/charset,MIT OR Apache-2.0,Henri Sivonen <hsivonen@hsivonen.fi>
chrono,https://github.com/chronotope/chrono,MIT OR Apache-2.0,The chrono Authors
chrono-tz,https://github.com/chronotope/chrono-tz,MIT OR Apache-2.0,The chrono-tz Authors
ciborium,https://github.com/enarx/ciborium,Apache-2.0,Nathaniel McCallum <npmccallum@profian.com>
ciborium-io,https://github.com/enarx/ciborium,Apache-2.0,Nathaniel McCallum <npmccallum@profian.com>
ciborium-ll,https://github.com/enarx/ciborium,Apache-2.0,Nathaniel McCallum <npmccallum@profian.com>
cidr,https://github.com/stbuehler/rust-cidr,MIT,Stefan Bühler <stbuehler@web.de>
cipher,https://github.com/RustCrypto/traits,MIT OR Apache-2.0,RustCrypto Developers
clap,https://github.com/clap-rs/clap,MIT OR Apache-2.0,The clap Authors
clap_builder,https://github.com/clap-rs/clap,MIT OR Apache-2.0,The clap_builder Authors
clap_complete,https://github.com/clap-rs/clap,MIT OR Apache-2.0,The clap_complete Authors
clap_derive,https://github.com/clap-rs/clap,MIT OR Apache-2.0,The clap_derive Authors
clap_lex,https://github.com/clap-rs/clap,MIT OR Apache-2.0,The clap_lex Authors
clipboard-win,https://github.com/DoumanAsh/clipboard-win,BSL-1.0,Douman <douman@gmx.se>
cmac,https://github.com/RustCrypto/MACs,MIT OR Apache-2.0,RustCrypto Developers
cmov,https://github.com/RustCrypto/utils,Apache-2.0 OR MIT,RustCrypto Developers
codespan-reporting,https://github.com/brendanzab/codespan,Apache-2.0,Brendan Zabarauskas <bjzaba@yahoo.com.au>
colorchoice,https://github.com/rust-cli/anstyle,MIT OR Apache-2.0,The colorchoice Authors
colored,https://github.com/mackwic/colored,MPL-2.0,Thomas Wickham <mackwic@gmail.com>
combine,https://github.com/Marwes/combine,MIT,Markus Westerlind <marwes91@gmail.com>
comfy-table,https://github.com/nukesor/comfy-table,MIT,Arne Beer <contact@arne.beer>
community-id,https://github.com/traceflight/rs-community-id,MIT OR Apache-2.0,Julian Wang <traceflight@outlook.com>
compact_str,https://github.com/ParkMyCar/compact_str,MIT,Parker Timmerman <parker@parkertimmerman.com>
compression-codecs,https://github.com/Nullus157/async-compression,MIT OR Apache-2.0,"Wim Looman <wim@nemo157.com>, Allen Bui <fairingrey@gmail.com>"
compression-core,https://github.com/Nullus157/async-compression,MIT OR Apache-2.0,"Wim Looman <wim@nemo157.com>, Allen Bui <fairingrey@gmail.com>"
concurrent-queue,https://github.com/smol-rs/concurrent-queue,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, Taiki Endo <te316e89@gmail.com>, John Nunley <dev@notgull.net>"
console-api,https://github.com/tokio-rs/console,MIT,"Eliza Weisman <eliza@buoyant.io>, Tokio Contributors <team@tokio.rs>"
console-subscriber,https://github.com/tokio-rs/console,MIT,"Eliza Weisman <eliza@buoyant.io>, Tokio Contributors <team@tokio.rs>"
const-oid,https://github.com/RustCrypto/formats,Apache-2.0 OR MIT,RustCrypto Developers
const-oid,https://github.com/RustCrypto/formats/tree/master/const-oid,Apache-2.0 OR MIT,RustCrypto Developers
const-random,https://github.com/tkaitchuck/constrandom,MIT OR Apache-2.0,Tom Kaitchuck <Tom.Kaitchuck@gmail.com>
const-random-macro,https://github.com/tkaitchuck/constrandom,MIT OR Apache-2.0,Tom Kaitchuck <Tom.Kaitchuck@gmail.com>
const-str,https://github.com/Nugine/const-str,MIT,Nugine <nugine@foxmail.com>
convert_case,https://github.com/rutrum/convert-case,MIT,rutrum <dave@rutrum.net>
cookie,https://github.com/SergioBenitez/cookie-rs,MIT OR Apache-2.0,"Sergio Benitez <sb@sergio.bz>, Alex Crichton <alex@alexcrichton.com>"
cookie-factory,https://github.com/rust-bakery/cookie-factory,MIT,"Geoffroy Couprie <geo.couprie@gmail.com>, Pierre Chifflier <chifflier@wzdftpd.net>"
cookie_store,https://github.com/pfernie/cookie_store,MIT OR Apache-2.0,Patrick Fernie <patrick.fernie@gmail.com>
core-foundation,https://github.com/servo/core-foundation-rs,MIT  OR  Apache-2.0,The Servo Project Developers
core-foundation,https://github.com/servo/core-foundation-rs,MIT OR Apache-2.0,The Servo Project Developers
core-foundation-sys,https://github.com/servo/core-foundation-rs,MIT OR Apache-2.0,The Servo Project Developers
core_detect,https://github.com/thomcc/core_detect,MIT OR Apache-2.0,Thom Chiovoloni <chiovolonit@gmail.com>
cpubits,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
cpufeatures,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
crc,https://github.com/mrhooray/crc-rs,MIT OR Apache-2.0,"Rui Hu <code@mrhooray.com>, Akhil Velagapudi <4@4khil.com>"
crc-catalog,https://github.com/akhilles/crc-catalog,MIT OR Apache-2.0,Akhil Velagapudi <akhilvelagapudi@gmail.com>
crc-fast,https://github.com/awesomized/crc-fast-rust,MIT OR Apache-2.0,Don MacAskill
crc32fast,https://github.com/srijs/rust-crc32fast,MIT OR Apache-2.0,"Sam Rijs <srijs@airpost.net>, Alex Crichton <alex@alexcrichton.com>"
critical-section,https://github.com/rust-embedded/critical-section,MIT OR Apache-2.0,The critical-section Authors
crossbeam-channel,https://github.com/crossbeam-rs/crossbeam,MIT OR Apache-2.0,The crossbeam-channel Authors
crossbeam-epoch,https://github.com/crossbeam-rs/crossbeam,MIT OR Apache-2.0,The crossbeam-epoch Authors
crossbeam-queue,https://github.com/crossbeam-rs/crossbeam,MIT OR Apache-2.0,The crossbeam-queue Authors
crossbeam-utils,https://github.com/crossbeam-rs/crossbeam,MIT OR Apache-2.0,The crossbeam-utils Authors
crossterm,https://github.com/crossterm-rs/crossterm,MIT,T. Post
crossterm_winapi,https://github.com/crossterm-rs/crossterm-winapi,MIT,T. Post
crunchy,https://github.com/eira-fransham/crunchy,MIT,Vurich <jackefransham@hotmail.co.uk>
crypto-bigint,https://github.com/RustCrypto/crypto-bigint,Apache-2.0 OR MIT,RustCrypto Developers
crypto-common,https://github.com/RustCrypto/traits,MIT OR Apache-2.0,RustCrypto Developers
crypto_secretbox,https://github.com/RustCrypto/nacl-compat/tree/master/crypto_secretbox,Apache-2.0 OR MIT,RustCrypto Developers
csv,https://github.com/BurntSushi/rust-csv,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
csv-core,https://github.com/BurntSushi/rust-csv,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
ctr,https://github.com/RustCrypto/block-modes,MIT OR Apache-2.0,RustCrypto Developers
ctutils,https://github.com/RustCrypto/utils,Apache-2.0 OR MIT,RustCrypto Developers
cuckoo-clock,https://github.com/Quad9DNS/cuckoo-clock,MIT,"John Todd <jtodd@quad9.net>, Ensar Sarajčić <dev@ensarsarajcic.com>"
curl-sys,https://github.com/alexcrichton/curl-rust,MIT,Alex Crichton <alex@alexcrichton.com>
curve25519-dalek,https://github.com/dalek-cryptography/curve25519-dalek/tree/main/curve25519-dalek,BSD-3-Clause,"Isis Lovecruft <isis@patternsinthevoid.net>, Henry de Valence <hdevalence@hdevalence.ca>"
curve25519-dalek-derive,https://github.com/dalek-cryptography/curve25519-dalek,MIT OR Apache-2.0,The curve25519-dalek-derive Authors
darling,https://github.com/TedDriggs/darling,MIT,Ted Driggs <ted.driggs@outlook.com>
darling_core,https://github.com/TedDriggs/darling,MIT,Ted Driggs <ted.driggs@outlook.com>
darling_macro,https://github.com/TedDriggs/darling,MIT,Ted Driggs <ted.driggs@outlook.com>
dashmap,https://github.com/xacrimon/dashmap,MIT,Acrimon <joel.wejdenstal@gmail.com>
data-encoding,https://github.com/ia0/data-encoding,MIT,The data-encoding Authors
data-url,https://github.com/servo/rust-url,MIT OR Apache-2.0,Simon Sapin <simon.sapin@exyr.org>
databend-client,https://github.com/databendlabs/bendsql,Apache-2.0,Databend Authors <opensource@databend.com>
databricks-zerobus-ingest-sdk,https://github.com/databricks/zerobus-sdk,Apache-2.0,Databricks
datadog-agent-metrics-v3,https://github.com/DataDog/saluki,Apache-2.0,The datadog-agent-metrics-v3 Authors
dbl,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
deadpool,https://github.com/deadpool-rs/deadpool,MIT OR Apache-2.0,Michael P. Jung <michael.jung@terreon.de>
deadpool-runtime,https://github.com/deadpool-rs/deadpool,MIT OR Apache-2.0,Michael P. Jung <michael.jung@terreon.de>
defmt,https://github.com/knurling-rs/defmt,MIT OR Apache-2.0,The Knurling-rs developers
defmt-macros,https://github.com/knurling-rs/defmt,MIT OR Apache-2.0,The Knurling-rs developers
defmt-parser,https://github.com/knurling-rs/defmt,MIT OR Apache-2.0,The Knurling-rs developers
der,https://github.com/RustCrypto/formats/tree/master/der,Apache-2.0 OR MIT,RustCrypto Developers
deranged,https://github.com/jhpratt/deranged,MIT OR Apache-2.0,Jacob Pratt <jacob@jhpratt.dev>
derivative,https://github.com/mcarton/rust-derivative,MIT OR Apache-2.0,mcarton <cartonmartin+git@gmail.com>
derive-syn-parse,https://github.com/sharnoff/derive-syn-parse,MIT OR Apache-2.0,sharnoff <git@sharnoff.io>
derive-where,https://github.com/ModProg/derive-where,MIT OR Apache-2.0,The derive-where Authors
derive_arbitrary,https://github.com/rust-fuzz/arbitrary,MIT OR Apache-2.0,"The Rust-Fuzz Project Developers, Nick Fitzgerald <fitzgen@gmail.com>, Manish Goregaokar <manishsmail@gmail.com>, Andre Bogus <bogusandre@gmail.com>, Corey Farwell <coreyf@rwell.org>"
derive_builder,https://github.com/colin-kiegel/rust-derive-builder,MIT OR Apache-2.0,"Colin Kiegel <kiegel@gmx.de>, Pascal Hertleif <killercup@gmail.com>, Jan-Erik Rediger <janerik@fnordig.de>, Ted Driggs <ted.driggs@outlook.com>"
derive_builder_core,https://github.com/colin-kiegel/rust-derive-builder,MIT OR Apache-2.0,"Colin Kiegel <kiegel@gmx.de>, Pascal Hertleif <killercup@gmail.com>, Jan-Erik Rediger <janerik@fnordig.de>, Ted Driggs <ted.driggs@outlook.com>"
derive_builder_macro,https://github.com/colin-kiegel/rust-derive-builder,MIT OR Apache-2.0,"Colin Kiegel <kiegel@gmx.de>, Pascal Hertleif <killercup@gmail.com>, Jan-Erik Rediger <janerik@fnordig.de>, Ted Driggs <ted.driggs@outlook.com>"
derive_more,https://github.com/JelteF/derive_more,MIT,Jelte Fennema <github-tech@jeltef.nl>
derive_more-impl,https://github.com/JelteF/derive_more,MIT,Jelte Fennema <github-tech@jeltef.nl>
digest,https://github.com/RustCrypto/traits,MIT OR Apache-2.0,RustCrypto Developers
dirs-next,https://github.com/xdg-rs/dirs,MIT OR Apache-2.0,The @xdg-rs members
dirs-sys-next,https://github.com/xdg-rs/dirs/tree/master/dirs-sys,MIT OR Apache-2.0,The @xdg-rs members
dispatch2,https://github.com/madsmtm/objc2,Zlib OR Apache-2.0 OR MIT,"Mads Marquart <mads@marquart.dk>, Mary <mary@mary.zone>"
displaydoc,https://github.com/yaahc/displaydoc,MIT OR Apache-2.0,Jane Lusby <jlusby@yaah.dev>
dns-lookup,https://github.com/keeperofdakeys/dns-lookup,MIT OR Apache-2.0,Josh Driver <keeperofdakeys@gmail.com>
doc-comment,https://github.com/GuillaumeGomez/doc-comment,MIT,Guillaume Gomez <guillaume1.gomez@gmail.com>
document-features,https://github.com/slint-ui/document-features,MIT OR Apache-2.0,Slint Developers <info@slint.dev>
domain,https://github.com/nlnetlabs/domain,BSD-3-Clause,NLnet Labs <dns-team@nlnetlabs.nl>
domain-macros,https://github.com/nlnetlabs/domain,BSD-3-Clause,NLnet Labs <dns-team@nlnetlabs.nl>
dotenvy,https://github.com/allan2/dotenvy,MIT,"Noemi Lapresta <noemi.lapresta@gmail.com>, Craig Hills <chills@gmail.com>, Mike Piccolo <mfpiccolo@gmail.com>, Alice Maz <alice@alicemaz.com>, Sean Griffin <sean@seantheprogrammer.com>, Adam Sharp <adam@sharplet.me>, Arpad Borsos <arpad.borsos@googlemail.com>, Allan Zhang <al@ayz.ai>"
dyn-clone,https://github.com/dtolnay/dyn-clone,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
ecdsa,https://github.com/RustCrypto/signatures/tree/master/ecdsa,Apache-2.0 OR MIT,RustCrypto Developers
ed25519,https://github.com/RustCrypto/signatures/tree/master/ed25519,Apache-2.0 OR MIT,RustCrypto Developers
ed25519-dalek,https://github.com/dalek-cryptography/curve25519-dalek/tree/main/ed25519-dalek,BSD-3-Clause,"isis lovecruft <isis@patternsinthevoid.net>, Tony Arcieri <bascule@gmail.com>, Michael Rosenberg <michael@mrosenberg.pub>"
educe,https://github.com/magiclen/educe,MIT,Magic Len <len@magiclen.org>
either,https://github.com/bluss/either,MIT OR Apache-2.0,bluss
elliptic-curve,https://github.com/RustCrypto/traits/tree/master/elliptic-curve,Apache-2.0 OR MIT,RustCrypto Developers
email_address,https://github.com/johnstonskj/rust-email_address,MIT,Simon Johnston <johnstonskj@gmail.com>
encode_unicode,https://github.com/tormol/encode_unicode,Apache-2.0 OR MIT,Torbjørn Birch Moltu <t.b.moltu@lyse.net>
encoding_rs,https://github.com/hsivonen/encoding_rs,(Apache-2.0 OR MIT) AND BSD-3-Clause,Henri Sivonen <hsivonen@hsivonen.fi>
encoding_rs_io,https://github.com/BurntSushi/encoding_rs_io,MIT OR Apache-2.0,Andrew Gallant <jamslam@gmail.com>
endian-type,https://github.com/Lolirofle/endian-type,MIT,Lolirofle <lolipopple@hotmail.com>
enum-ordinalize,https://github.com/magiclen/enum-ordinalize,MIT,The enum-ordinalize Authors
enum-ordinalize-derive,https://github.com/magiclen/enum-ordinalize,MIT,The enum-ordinalize-derive Authors
enum_dispatch,https://gitlab.com/antonok/enum_dispatch,MIT OR Apache-2.0,Anton Lazarev <https://antonok.com>
enumflags2,https://github.com/meithecatte/enumflags2,MIT OR Apache-2.0,"maik klein <maikklein@googlemail.com>, Maja Kądziołka <maya@compilercrim.es>"
enumflags2_derive,https://github.com/meithecatte/enumflags2,MIT OR Apache-2.0,"maik klein <maikklein@googlemail.com>, Maja Kądziołka <maya@compilercrim.es>"
env_filter,https://github.com/rust-cli/env_logger,MIT OR Apache-2.0,The env_filter Authors
env_logger,https://github.com/rust-cli/env_logger,MIT OR Apache-2.0,The env_logger Authors
equivalent,https://github.com/cuviper/equivalent,Apache-2.0 OR MIT,The equivalent Authors
erased-serde,https://github.com/dtolnay/erased-serde,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
errno,https://github.com/lambda-fairy/rust-errno,MIT OR Apache-2.0,Chris Wong <lambda.fairy@gmail.com>
error-chain,https://github.com/rust-lang-nursery/error-chain,MIT OR Apache-2.0,"Brian Anderson <banderson@mozilla.com>, Paul Colomiets <paul@colomiets.name>, Colin Kiegel <kiegel@gmx.de>, Yamakaky <yamakaky@yamaworld.fr>, Andrew Gauger <andygauge@gmail.com>"
error-code,https://github.com/DoumanAsh/error-code,BSL-1.0,Douman <douman@gmx.se>
etcetera,https://github.com/lunacookies/etcetera,MIT OR Apache-2.0,The etcetera Authors
event-listener,https://github.com/smol-rs/event-listener,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
event-listener,https://github.com/smol-rs/event-listener,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, John Nunley <dev@notgull.net>"
event-listener-strategy,https://github.com/smol-rs/event-listener-strategy,Apache-2.0 OR MIT,John Nunley <dev@notgull.net>
evmap,https://github.com/jonhoo/rust-evmap,MIT OR Apache-2.0,Jon Gjengset <jon@thesquareplanet.com>
evmap-derive,https://github.com/jonhoo/rust-evmap,MIT OR Apache-2.0,Jon Gjengset <jon@thesquareplanet.com>
exitcode,https://github.com/benwilber/exitcode,Apache-2.0,Ben Wilber <benwilber@gmail.com>
fallible-iterator,https://github.com/sfackler/rust-fallible-iterator,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
fancy-regex,https://github.com/fancy-regex/fancy-regex,MIT,"Raph Levien <raph@google.com>, Robin Stocker <robin@nibor.org>, Keith Hall <keith.hall@available.systems>"
fastrand,https://github.com/smol-rs/fastrand,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
ff,https://github.com/zkcrypto/ff,MIT OR Apache-2.0,"Sean Bowe <ewillbefull@gmail.com>, Jack Grigg <thestr4d@gmail.com>"
fiat-crypto,https://github.com/mit-plv/fiat-crypto,MIT OR Apache-2.0 OR BSD-1-Clause,Fiat Crypto library authors <jgross@mit.edu>
finl_unicode,https://github.com/dahosek/finl_unicode,MIT OR Apache-2.0,The finl_unicode Authors
flatbuffers,https://github.com/google/flatbuffers,Apache-2.0,"Robert Winslow <hello@rwinslow.com>, FlatBuffers Maintainers"
flate2,https://github.com/rust-lang/flate2-rs,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Josh Triplett <josh@joshtriplett.org>"
float_eq,https://github.com/jtempest/float_eq-rs,MIT OR Apache-2.0,jtempest
fluent-uri,https://github.com/yescallop/fluent-uri-rs,MIT,Scallop Ye <yescallop@gmail.com>
flume,https://github.com/zesterer/flume,Apache-2.0 OR MIT,Joshua Barretto <joshua.s.barretto@gmail.com>
fnv,https://github.com/servo/rust-fnv,Apache-2.0  OR  MIT,Alex Crichton <alex@alexcrichton.com>
foldhash,https://github.com/orlp/foldhash,Zlib,Orson Peters <orsonpeters@gmail.com>
foreign-types,https://github.com/sfackler/foreign-types,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
foreign-types-shared,https://github.com/sfackler/foreign-types,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
form_urlencoded,https://github.com/servo/rust-url,MIT OR Apache-2.0,The rust-url developers
fraction,https://github.com/dnsl48/fraction,MIT OR Apache-2.0,dnsl48 <dnsl48@gmail.com>
fsevent-sys,https://github.com/octplane/fsevent-rust/tree/master/fsevent-sys,MIT,Pierre Baillet <pierre@baillet.name>
fslock,https://github.com/brunoczim/fslock,MIT,The fslock Authors
funty,https://github.com/myrrlyn/funty,MIT,myrrlyn <self@myrrlyn.dev>
futures,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures Authors
futures-channel,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-channel Authors
futures-core,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-core Authors
futures-executor,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-executor Authors
futures-intrusive,https://github.com/Matthias247/futures-intrusive,MIT OR Apache-2.0,Matthias Einwag <matthias.einwag@live.com>
futures-io,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-io Authors
futures-lite,https://github.com/smol-rs/futures-lite,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, Contributors to futures-rs"
futures-macro,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-macro Authors
futures-sink,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-sink Authors
futures-task,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-task Authors
futures-timer,https://github.com/async-rs/futures-timer,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
futures-util,https://github.com/rust-lang/futures-rs,MIT OR Apache-2.0,The futures-util Authors
generic-array,https://github.com/fizyk20/generic-array,MIT,"Bartłomiej Kamiński <fizyk20@gmail.com>, Aaron Trent <novacrazy@gmail.com>"
getrandom,https://github.com/rust-random/getrandom,MIT OR Apache-2.0,The Rand Project Developers
glob,https://github.com/rust-lang/glob,MIT OR Apache-2.0,The Rust Project Developers
gloo-timers,https://github.com/rustwasm/gloo/tree/master/crates/timers,MIT OR Apache-2.0,Rust and WebAssembly Working Group
goauth,https://github.com/durch/rust-goauth,MIT,Drazen Urch <github@drazenur.ch>
governor,https://github.com/boinkor-net/governor,MIT,Andreas Fuchs <asf@boinkor.net>
granit-parser,https://github.com/bourumir-wyngs/granit-parser,MIT OR Apache-2.0,"Ethiraric <ethiraric@gmail.com>, David Aguilar <davvid@gmail.com>, Yuheng Chen <yuhengchen@sensetime.com>, Bourumir Wyngs <bourumir-wyngs@gmail.com>"
greptime-proto,https://github.com/GreptimeTeam/greptime-proto,Apache-2.0,The greptime-proto Authors
greptimedb-ingester,https://github.com/GreptimeTeam/greptimedb-ingester-rust,Apache-2.0,The greptimedb-ingester Authors
grok,https://github.com/mmastrac/grok,Apache-2.0,"Matt Mastracci <matthew@mastracci.com>, Michael Nitschinger <michael@nitschinger.at>"
group,https://github.com/zkcrypto/group,MIT OR Apache-2.0,"Sean Bowe <ewillbefull@gmail.com>, Jack Grigg <jack@z.cash>"
h2,https://github.com/hyperium/h2,MIT,"Carl Lerche <me@carllerche.com>, Sean McArthur <sean@seanmonstar.com>"
half,https://github.com/starkat99/half-rs,MIT OR Apache-2.0,Kathryn Long <squeeself@gmail.com>
hash_hasher,https://github.com/Fraser999/Hash-Hasher,Apache-2.0 OR MIT,Fraser Hutchison <fraser@astria.org>
hashbag,https://github.com/jonhoo/hashbag,MIT OR Apache-2.0,Jon Gjengset <jon@thesquareplanet.com>
hashbrown,https://github.com/rust-lang/hashbrown,MIT OR Apache-2.0,Amanieu d'Antras <amanieu@gmail.com>
hashbrown,https://github.com/rust-lang/hashbrown,MIT OR Apache-2.0,The hashbrown Authors
hashlink,https://github.com/kyren/hashlink,MIT OR Apache-2.0,kyren <kerriganw@gmail.com>
hdrhistogram,https://github.com/HdrHistogram/HdrHistogram_rust,MIT OR Apache-2.0,"Jon Gjengset <jon@thesquareplanet.com>, Marshall Pierce <marshall@mpierce.org>"
headers,https://github.com/hyperium/headers,MIT,Sean McArthur <sean@seanmonstar.com>
headers-core,https://github.com/hyperium/headers,MIT,Sean McArthur <sean@seanmonstar.com>
heck,https://github.com/withoutboats/heck,MIT OR Apache-2.0,The heck Authors
heck,https://github.com/withoutboats/heck,MIT OR Apache-2.0,Without Boats <woboats@gmail.com>
heim,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-common,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-cpu,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-disk,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-host,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-memory,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-net,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
heim-runtime,https://github.com/heim-rs/heim,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
hermit-abi,https://github.com/hermit-os/hermit-rs,MIT OR Apache-2.0,Stefan Lankes
hex,https://github.com/KokaKiwi/rust-hex,MIT OR Apache-2.0,KokaKiwi <kokakiwi@kokakiwi.net>
hickory-net,https://github.com/hickory-dns/hickory-dns,MIT OR Apache-2.0,The contributors to Hickory DNS
hickory-proto,https://github.com/hickory-dns/hickory-dns,MIT OR Apache-2.0,The contributors to Hickory DNS
hickory-resolver,https://github.com/hickory-dns/hickory-dns,MIT OR Apache-2.0,The contributors to Hickory DNS
hkdf,https://github.com/RustCrypto/KDFs,MIT OR Apache-2.0,RustCrypto Developers
hmac,https://github.com/RustCrypto/MACs,MIT OR Apache-2.0,RustCrypto Developers
home,https://github.com/rust-lang/cargo,MIT OR Apache-2.0,Brian Anderson <andersrb@gmail.com>
hostname,https://github.com/djc/hostname,MIT,The hostname Authors
hostname,https://github.com/svartalf/hostname,MIT,"fengcen <fengcen.love@gmail.com>, svartalf <self@svartalf.info>"
http,https://github.com/hyperium/http,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Carl Lerche <me@carllerche.com>, Sean McArthur <sean@seanmonstar.com>"
http-body,https://github.com/hyperium/http-body,MIT,"Carl Lerche <me@carllerche.com>, Lucio Franco <luciofranco14@gmail.com>, Sean McArthur <sean@seanmonstar.com>"
http-body-util,https://github.com/hyperium/http-body,MIT,"Carl Lerche <me@carllerche.com>, Lucio Franco <luciofranco14@gmail.com>, Sean McArthur <sean@seanmonstar.com>"
http-range-header,https://github.com/MarcusGrass/parse-range-headers,MIT,The http-range-header Authors
http-serde,https://gitlab.com/kornelski/http-serde,Apache-2.0 OR MIT,Kornel <kornel@geekhood.net>
httparse,https://github.com/seanmonstar/httparse,MIT OR Apache-2.0,Sean McArthur <sean@seanmonstar.com>
httpdate,https://github.com/pyfisch/httpdate,MIT OR Apache-2.0,Pyfisch <pyfisch@posteo.org>
humantime,https://github.com/chronotope/humantime,MIT OR Apache-2.0,The humantime Authors
hybrid-array,https://github.com/RustCrypto/hybrid-array,MIT OR Apache-2.0,RustCrypto Developers
hyper,https://github.com/hyperium/hyper,MIT,Sean McArthur <sean@seanmonstar.com>
hyper-http-proxy,https://github.com/metalbear-co/hyper-http-proxy,MIT,MetalBear Tech LTD <hi@metalbear.co>
hyper-named-pipe,https://github.com/fussybeaver/hyper-named-pipe,Apache-2.0,The hyper-named-pipe Authors
hyper-openssl,https://github.com/sfackler/hyper-openssl,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
hyper-proxy,https://github.com/tafia/hyper-proxy,MIT,Johann Tuffe <tafia973@gmail.com>
hyper-rustls,https://github.com/rustls/hyper-rustls,Apache-2.0 OR ISC OR MIT,The hyper-rustls Authors
hyper-timeout,https://github.com/hjr3/hyper-timeout,MIT OR Apache-2.0,Herman J. Radtke III <herman@hermanradtke.com>
hyper-tls,https://github.com/hyperium/hyper-tls,MIT OR Apache-2.0,Sean McArthur <sean@seanmonstar.com>
hyper-util,https://github.com/hyperium/hyper-util,MIT,Sean McArthur <sean@seanmonstar.com>
hyperlocal,https://github.com/softprops/hyperlocal,MIT,softprops <d.tangren@gmail.com>
iana-time-zone,https://github.com/strawlab/iana-time-zone,MIT OR Apache-2.0,"Andrew Straw <strawman@astraw.com>, René Kijewski <rene.kijewski@fu-berlin.de>, Ryan Lopopolo <rjl@hyperbo.la>"
iana-time-zone-haiku,https://github.com/strawlab/iana-time-zone,MIT OR Apache-2.0,René Kijewski <crates.io@k6i.de>
icu_collections,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_locid,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_locid_transform,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_locid_transform_data,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_normalizer,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_normalizer_data,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_properties,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_properties_data,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_provider,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
icu_provider_macros,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
id-arena,https://github.com/fitzgen/id-arena,MIT OR Apache-2.0,"Nick Fitzgerald <fitzgen@gmail.com>, Aleksey Kladov <aleksey.kladov@gmail.com>"
ident_case,https://github.com/TedDriggs/ident_case,MIT OR Apache-2.0,Ted Driggs <ted.driggs@outlook.com>
idna,https://github.com/servo/rust-url,MIT OR Apache-2.0,The rust-url developers
idna_adapter,https://github.com/hsivonen/idna_adapter,Apache-2.0 OR MIT,The rust-url developers
indexmap,https://github.com/bluss/indexmap,Apache-2.0 OR MIT,The indexmap Authors
indexmap,https://github.com/indexmap-rs/indexmap,Apache-2.0 OR MIT,The indexmap Authors
indoc,https://github.com/dtolnay/indoc,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
influxdb-line-protocol,https://github.com/influxdata/influxdb_iox/tree/main/influxdb_line_protocol,MIT OR Apache-2.0,InfluxDB IOx Project Developers
inotify,https://github.com/hannobraun/inotify,ISC,"Hanno Braun <mail@hannobraun.de>, Félix Saparelli <me@passcod.name>, Cristian Kubis <cristian.kubis@tsunix.de>, Frank Denis <github@pureftpd.org>"
inotify-sys,https://github.com/hannobraun/inotify-sys,ISC,Hanno Braun <hb@hannobraun.de>
inout,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
instability,https://github.com/ratatui-org/instability,MIT,"Stephen M. Coakley <me@stephencoakley.com>, Joshka"
inventory,https://github.com/dtolnay/inventory,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
ipconfig,https://github.com/liranringel/ipconfig,MIT OR Apache-2.0,Liran Ringel <liranringel@gmail.com>
ipcrypt-rs,https://github.com/jedisct1/rust-ipcrypt2,ISC,Frank Denis <github@pureftpd.org>
ipnet,https://github.com/krisprice/ipnet,MIT OR Apache-2.0,Kris Price <kris@krisprice.nz>
ipnetwork,https://github.com/achanda/ipnetwork,MIT OR Apache-2.0,"Abhishek Chanda <abhishek.becs@gmail.com>, Linus Färnstrand <faern@faern.net>"
iri-string,https://github.com/lo48576/iri-string,MIT OR Apache-2.0,YOSHIOKA Takuma <nop_thread@nops.red>
is-terminal,https://github.com/sunfishcode/is-terminal,MIT,"softprops <d.tangren@gmail.com>, Dan Gohman <dev@sunfishcode.online>"
is_ci,https://github.com/zkat/is_ci,ISC,Kat Marchán <kzm@zkat.tech>
is_terminal_polyfill,https://github.com/polyfill-rs/is_terminal_polyfill,MIT OR Apache-2.0,The is_terminal_polyfill Authors
itertools,https://github.com/rust-itertools/itertools,MIT OR Apache-2.0,bluss
itoa,https://github.com/dtolnay/itoa,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
jiff,https://github.com/BurntSushi/jiff,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
jiff-core,https://github.com/BurntSushi/jiff,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
jiff-static,https://github.com/BurntSushi/jiff,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
jiff-tzdb,https://github.com/BurntSushi/jiff,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
jiff-tzdb-platform,https://github.com/BurntSushi/jiff,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
jni,https://github.com/jni-rs/jni-rs,MIT OR Apache-2.0,jni team
jni-macros,https://github.com/jni-rs/jni-rs,MIT OR Apache-2.0,The jni-macros Authors
jni-sys,https://github.com/jni-rs/jni-sys,MIT OR Apache-2.0,"Steven Fackler <sfackler@gmail.com>, Robert Bragg <robert@sixbynine.org>"
jni-sys-macros,https://github.com/jni-rs/jni-sys,MIT OR Apache-2.0,Robert Bragg <robert@sixbynine.org>
js-sys,https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/js-sys,MIT OR Apache-2.0,The wasm-bindgen Developers
json-patch,https://github.com/idubrov/json-patch,MIT OR Apache-2.0,Ivan Dubrov <dubrov.ivan@gmail.com>
jsonpath-rust,https://github.com/besok/jsonpath-rust,MIT,BorisZhguchev <zhguchev@gmail.com>
jsonptr,https://github.com/chanced/jsonptr,MIT OR Apache-2.0,"chance dinkins, André Sá de Mello <codasm@pm.me>, Oliver Wangler <oliver@wngr.de>"
jsonschema,https://github.com/Stranger6667/jsonschema,MIT,Dmitry Dygalo <dmitry@dygalo.dev>
k8s-openapi,https://github.com/Arnavion/k8s-openapi,Apache-2.0,Arnav Singh <me@arnavion.dev>
kasuari,https://github.com/ratatui/kasuari,MIT OR Apache-2.0,"Dylan Ede <dylanede@googlemail.com>, The Ratatui Developers"
keccak,https://github.com/RustCrypto/sponges,Apache-2.0 OR MIT,RustCrypto Developers
kqueue,https://gitlab.com/rust-kqueue/rust-kqueue,MIT,William Orr <will@worrbase.com>
kqueue-sys,https://gitlab.com/rust-kqueue/rust-kqueue-sys,MIT,"William Orr <will@worrbase.com>, Daniel (dmilith) Dettlaff <dmilith@me.com>"
krb5-src,https://github.com/MaterializeInc/rust-krb5-src,Apache-2.0,"Materialize, Inc."
kube,https://github.com/kube-rs/kube,Apache-2.0,"clux <sszynrae@gmail.com>, Natalie Klestrup Röijezon <nat@nullable.se>, kazk <kazk.dev@gmail.com>"
kube-client,https://github.com/kube-rs/kube,Apache-2.0,"clux <sszynrae@gmail.com>, Natalie Klestrup Röijezon <nat@nullable.se>, kazk <kazk.dev@gmail.com>"
kube-core,https://github.com/kube-rs/kube,Apache-2.0,"clux <sszynrae@gmail.com>, Natalie Klestrup Röijezon <nat@nullable.se>, kazk <kazk.dev@gmail.com>"
kube-runtime,https://github.com/kube-rs/kube,Apache-2.0,"clux <sszynrae@gmail.com>, Natalie Klestrup Röijezon <nat@nullable.se>, kazk <kazk.dev@gmail.com>"
lalrpop-util,https://github.com/lalrpop/lalrpop,Apache-2.0 OR MIT,Niko Matsakis <niko@alum.mit.edu>
lapin,https://github.com/amqp-rs/lapin,MIT,"Geoffroy Couprie <geo.couprie@gmail.com>, Marc-Antoine Perennou <Marc-Antoine@Perennou.com>"
lazy_static,https://github.com/rust-lang-nursery/lazy-static.rs,MIT OR Apache-2.0,Marvin Löbel <loebel.marvin@gmail.com>
leb128fmt,https://github.com/bluk/leb128fmt,MIT OR Apache-2.0,Bryant Luk <code@bryantluk.com>
lexical-core,https://github.com/Alexhuszagh/rust-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
lexical-parse-float,https://github.com/Alexhuszagh/rust-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
lexical-parse-integer,https://github.com/Alexhuszagh/rust-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
lexical-util,https://github.com/Alexhuszagh/rust-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
lexical-write-float,https://github.com/Alexhuszagh/rust-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
lexical-write-integer,https://github.com/Alexhuszagh/rust-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
libc,https://github.com/rust-lang/libc,MIT OR Apache-2.0,The libc Authors
libloading,https://github.com/nagisa/rust_libloading,ISC,Simonas Kazlauskas <libloading@kazlauskas.me>
libm,https://github.com/rust-lang/libm,MIT OR Apache-2.0,Jorge Aparicio <jorge@japaric.io>
libredox,https://gitlab.redox-os.org/redox-os/libredox,MIT,4lDO2 <4lDO2@protonmail.com>
libsqlite3-sys,https://github.com/rusqlite/rusqlite,MIT,The rusqlite developers
libz-sys,https://github.com/rust-lang/libz-sys,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Josh Triplett <josh@joshtriplett.org>, Sebastian Thiel <sebastian.thiel@icloud.com>"
line-clipping,https://github.com/joshka/line-clipping,MIT OR Apache-2.0,Josh McKinney
linked-hash-map,https://github.com/contain-rs/linked-hash-map,MIT OR Apache-2.0,"Stepan Koltsov <stepan.koltsov@gmail.com>, Andrew Paseltiner <apaseltiner@gmail.com>"
linked_hash_set,https://github.com/alexheretic/linked-hash-set,Apache-2.0,Alex Butler <alexheretic@gmail.com>
linkme,https://github.com/dtolnay/linkme,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
linkme-impl,https://github.com/dtolnay/linkme,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
linux-raw-sys,https://github.com/sunfishcode/linux-raw-sys,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Dan Gohman <dev@sunfishcode.online>
listenfd,https://github.com/mitsuhiko/listenfd,Apache-2.0,Armin Ronacher <armin.ronacher@active-4.com>
litemap,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
litrs,https://github.com/LukasKalbertodt/litrs,MIT OR Apache-2.0,Lukas Kalbertodt <lukas.kalbertodt@gmail.com>
lock_api,https://github.com/Amanieu/parking_lot,MIT OR Apache-2.0,Amanieu d'Antras <amanieu@gmail.com>
lockfree-object-pool,https://github.com/EVaillant/lockfree-object-pool,BSL-1.0,Etienne Vaillant <vaillant.etienne@gmail.com>
log,https://github.com/rust-lang/log,MIT OR Apache-2.0,The Rust Project Developers
lru,https://github.com/jeromefroe/lru-rs,MIT,Jerome Froelich <jeromefroelic@hotmail.com>
lru-slab,https://github.com/Ralith/lru-slab,MIT OR Apache-2.0 OR Zlib,Benjamin Saunders <ben.e.saunders@gmail.com>
lz4,https://github.com/10xGenomics/lz4-rs,MIT,"Jens Heyens <jens.heyens@ewetel.net>, Artem V. Navrotskiy <bozaro@buzzsoft.ru>, Patrick Marks <pmarks@gmail.com>"
lz4-sys,https://github.com/10xGenomics/lz4-rs,MIT,"Jens Heyens <jens.heyens@ewetel.net>, Artem V. Navrotskiy <bozaro@buzzsoft.ru>, Patrick Marks <pmarks@gmail.com>"
lz4_flex,https://github.com/pseitz/lz4_flex,MIT,"Pascal Seitz <pascal.seitz@gmail.com>, Arthur Silva <arthurprs@gmail.com>, ticki <Ticki@users.noreply.github.com>"
macaddr,https://github.com/svartalf/rust-macaddr,Apache-2.0 OR MIT,svartalf <self@svartalf.info>
mach2,https://github.com/JohnTitor/mach2,BSD-2-Clause OR MIT OR Apache-2.0,The mach2 Authors
macro_magic,https://github.com/sam0x17/macro_magic,MIT,sam0x17
macro_magic_core,https://github.com/sam0x17/macro_magic,MIT,The macro_magic_core Authors
macro_magic_core_macros,https://github.com/sam0x17/macro_magic,MIT,The macro_magic_core_macros Authors
macro_magic_macros,https://github.com/sam0x17/macro_magic,MIT,The macro_magic_macros Authors
match_cfg,https://github.com/gnzlbg/match_cfg,MIT OR Apache-2.0,gnzlbg <gonzalobg88@gmail.com>
matchers,https://github.com/hawkw/matchers,MIT,Eliza Weisman <eliza@buoyant.io>
matchit,https://github.com/ibraheemdev/matchit,MIT AND BSD-3-Clause,Ibraheem Ahmed <ibraheem@ibraheem.ca>
maxminddb,https://github.com/oschwald/maxminddb-rust,ISC,Gregory J. Oschwald <oschwald@gmail.com>
md-5,https://github.com/RustCrypto/hashes,MIT OR Apache-2.0,RustCrypto Developers
memchr,https://github.com/BurntSushi/memchr,Unlicense OR MIT,"Andrew Gallant <jamslam@gmail.com>, bluss"
memmap2,https://github.com/RazrFalcon/memmap2-rs,MIT OR Apache-2.0,"Dan Burkert <dan@danburkert.com>, Yevhenii Reizner <razrfalcon@gmail.com>, The Contributors"
memoffset,https://github.com/Gilnaa/memoffset,MIT,Gilad Naaman <gilad.naaman@gmail.com>
metrics,https://github.com/metrics-rs/metrics,MIT,Toby Lawrence <toby@nuclearfurnace.com>
metrics-tracing-context,https://github.com/metrics-rs/metrics,MIT,MOZGIII <mike-n@narod.ru>
metrics-util,https://github.com/metrics-rs/metrics,MIT,Toby Lawrence <toby@nuclearfurnace.com>
mime,https://github.com/hyperium/mime,MIT OR Apache-2.0,Sean McArthur <sean@seanmonstar.com>
mime_guess,https://github.com/abonander/mime_guess,MIT,Austin Bonander <austin.bonander@gmail.com>
minicbor,https://github.com/twittner/minicbor,BlueOak-1.0.0,Toralf Wittner <tw@dtex.org>
minimal-lexical,https://github.com/Alexhuszagh/minimal-lexical,MIT OR Apache-2.0,Alex Huszagh <ahuszagh@gmail.com>
miniz_oxide,https://github.com/Frommi/miniz_oxide/tree/master/miniz_oxide,MIT OR Zlib OR Apache-2.0,"Frommi <daniil.liferenko@gmail.com>, oyvindln <oyvindln@users.noreply.github.com>, Rich Geldreich richgel99@gmail.com"
mio,https://github.com/tokio-rs/mio,MIT,"Carl Lerche <me@carllerche.com>, Thomas de Zeeuw <thomasdezeeuw@gmail.com>, Tokio Contributors <team@tokio.rs>"
mlua,https://github.com/mlua-rs/mlua,MIT,"Aleksandr Orlenko <zxteam@pm.me>, kyren <catherine@kyju.org>"
mlua-sys,https://github.com/mlua-rs/mlua,MIT,Aleksandr Orlenko <zxteam@pm.me>
mlua_derive,https://github.com/mlua-rs/mlua,MIT,Aleksandr Orlenko <zxteam@pm.me>
moka,https://github.com/moka-rs/moka,MIT OR Apache-2.0,The moka Authors
mongocrypt,https://github.com/mongodb/libmongocrypt-rust,Apache-2.0,"Abraham Egnor <abraham.egnor@mongodb.com>, Isabel Atkinson <isabel.atkinson@mongodb.com>"
mongocrypt-sys,https://github.com/mongodb/libmongocrypt-rust,Apache-2.0,"Abraham Egnor <abraham.egnor@mongodb.com>, Isabel Atkinson <isabel.atkinson@mongodb.com>"
mongodb,https://github.com/mongodb/mongo-rust-driver,Apache-2.0,"Saghm Rossi <saghmrossi@gmail.com>, Patrick Freed <patrick.freed@mongodb.com>, Isabel Atkinson <isabel.atkinson@mongodb.com>, Abraham Egnor <abraham.egnor@mongodb.com>, Kaitlin Mahar <kaitlin.mahar@mongodb.com>, Patrick Meredith <pmeredit@protonmail.com>"
mongodb-internal-macros,https://github.com/mongodb/mongo-rust-driver,Apache-2.0,"Saghm Rossi <saghmrossi@gmail.com>, Patrick Freed <patrick.freed@mongodb.com>, Isabel Atkinson <isabel.atkinson@mongodb.com>, Abraham Egnor <abraham.egnor@mongodb.com>, Kaitlin Mahar <kaitlin.mahar@mongodb.com>, Patrick Meredith <pmeredit@protonmail.com>"
multiversion,https://github.com/calebzulawski/multiversion,MIT OR Apache-2.0,Caleb Zulawski <caleb.zulawski@gmail.com>
multiversion-macros,https://github.com/calebzulawski/multiversion,MIT OR Apache-2.0,Caleb Zulawski <caleb.zulawski@gmail.com>
multiversion_no_op,https://github.com/hsivonen/multiversion_no_op,Apache-2.0 OR MIT,Henri Sivonen <hsivonen@hsivonen.fi>
murmur3,https://github.com/stusmall/murmur3,MIT OR Apache-2.0,Stu Small <stuart.alan.small@gmail.com>
native-tls,https://github.com/rust-native-tls/rust-native-tls,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
ndk-context,https://github.com/rust-windowing/android-ndk-rs,MIT OR Apache-2.0,The Rust Windowing contributors
newtype-uuid,https://github.com/oxidecomputer/newtype-uuid,MIT OR Apache-2.0,The newtype-uuid Authors
nibble_vec,https://github.com/michaelsproul/rust_nibble_vec,MIT,Michael Sproul <micsproul@gmail.com>
nix,https://github.com/nix-rust/nix,MIT,The nix Authors
nix,https://github.com/nix-rust/nix,MIT,The nix-rust Project Developers
nkeys,https://github.com/wasmcloud/nkeys,Apache-2.0,wasmCloud Team
nohash,https://github.com/tetcoin/nohash,Apache-2.0 OR MIT,Parity Technologies <admin@parity.io>
nohash-hasher,https://github.com/paritytech/nohash-hasher,Apache-2.0 OR MIT,Parity Technologies <admin@parity.io>
nom,https://github.com/Geal/nom,MIT,contact@geoffroycouprie.com
nom,https://github.com/rust-bakery/nom,MIT,contact@geoffroycouprie.com
nom-language,https://github.com/rust-bakery/nom,MIT,contact@geoffroycouprie.com
nonzero_ext,https://github.com/antifuchs/nonzero_ext,Apache-2.0,Andreas Fuchs <asf@boinkor.net>
notify,https://github.com/notify-rs/notify,CC0-1.0,"Félix Saparelli <me@passcod.name>, Daniel Faust <hessijames@gmail.com>, Aron Heinecke <Ox0p54r36@t-online.de>"
notify-types,https://github.com/notify-rs/notify,MIT OR Apache-2.0,Daniel Faust <hessijames@gmail.com>
ntapi,https://github.com/MSxDOS/ntapi,Apache-2.0 OR MIT,MSxDOS <melcodos@gmail.com>
nu-ansi-term,https://github.com/nushell/nu-ansi-term,MIT,"ogham@bsago.me, Ryan Scheel (Havvy) <ryan.havvy@gmail.com>, Josh Triplett <josh@joshtriplett.org>, The Nushell Project Developers"
num,https://github.com/rust-num/num,MIT OR Apache-2.0,The Rust Project Developers
num-bigint,https://github.com/rust-num/num-bigint,MIT OR Apache-2.0,The Rust Project Developers
num-bigint,https://github.com/rust-num/num-bigint,MIT OR Apache-2.0,The num-bigint Authors
num-bigint-dig,https://github.com/dignifiedquire/num-bigint,MIT OR Apache-2.0,"dignifiedquire <dignifiedquire@gmail.com>, The Rust Project Developers"
num-cmp,https://github.com/lifthrasiir/num-cmp,MIT OR Apache-2.0,Kang Seonghoon <public+git@mearie.org>
num-complex,https://github.com/rust-num/num-complex,MIT OR Apache-2.0,The Rust Project Developers
num-conv,https://github.com/jhpratt/num-conv,MIT OR Apache-2.0,Jacob Pratt <jacob@jhpratt.dev>
num-format,https://github.com/bcmyers/num-format,MIT OR Apache-2.0,Brian Myers <brian.carl.myers@gmail.com>
num-integer,https://github.com/rust-num/num-integer,MIT OR Apache-2.0,The Rust Project Developers
num-iter,https://github.com/rust-num/num-iter,MIT OR Apache-2.0,The Rust Project Developers
num-rational,https://github.com/rust-num/num-rational,MIT OR Apache-2.0,The Rust Project Developers
num-traits,https://github.com/rust-num/num-traits,MIT OR Apache-2.0,The Rust Project Developers
num_enum,https://github.com/illicitonion/num_enum,BSD-3-Clause OR MIT OR Apache-2.0,"Daniel Wagner-Hall <dawagner@gmail.com>, Daniel Henry-Mantilla <daniel.henry.mantilla@gmail.com>, Vincent Esche <regexident@gmail.com>"
num_enum_derive,https://github.com/illicitonion/num_enum,BSD-3-Clause OR MIT OR Apache-2.0,"Daniel Wagner-Hall <dawagner@gmail.com>, Daniel Henry-Mantilla <daniel.henry.mantilla@gmail.com>, Vincent Esche <regexident@gmail.com>"
num_threads,https://github.com/jhpratt/num_threads,MIT OR Apache-2.0,Jacob Pratt <open-source@jhpratt.dev>
oauth2,https://github.com/ramosbugs/oauth2-rs,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Florin Lipan <florinlipan@gmail.com>, David A. Ramos <ramos@cs.stanford.edu>"
objc2,https://github.com/madsmtm/objc2,MIT,Mads Marquart <mads@marquart.dk>
objc2-app-kit,https://github.com/madsmtm/objc2,Zlib OR Apache-2.0 OR MIT,The objc2-app-kit Authors
objc2-core-foundation,https://github.com/madsmtm/objc2,Zlib OR Apache-2.0 OR MIT,The objc2-core-foundation Authors
objc2-encode,https://github.com/madsmtm/objc2,MIT,Mads Marquart <mads@marquart.dk>
objc2-foundation,https://github.com/madsmtm/objc2,MIT,The objc2-foundation Authors
objc2-io-kit,https://github.com/madsmtm/objc2,Zlib OR Apache-2.0 OR MIT,The objc2-io-kit Authors
objc2-system-configuration,https://github.com/madsmtm/objc2,Zlib OR Apache-2.0 OR MIT,The objc2-system-configuration Authors
octseq,https://github.com/NLnetLabs/octets,BSD-3-Clause,NLnet Labs <rust-team@nlnetlabs.nl>
ofb,https://github.com/RustCrypto/block-modes,MIT OR Apache-2.0,RustCrypto Developers
once_cell,https://github.com/matklad/once_cell,MIT OR Apache-2.0,Aleksey Kladov <aleksey.kladov@gmail.com>
once_cell_polyfill,https://github.com/polyfill-rs/once_cell_polyfill,MIT OR Apache-2.0,The once_cell_polyfill Authors
onig,https://github.com/iwillspeak/rust-onig,MIT,"Will Speak <will@willspeak.me>, Ivan Ivashchenko <defuz@me.com>"
onig_sys,https://github.com/iwillspeak/rust-onig,MIT,"Will Speak <will@willspeak.me>, Ivan Ivashchenko <defuz@me.com>"
opaque-debug,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
opendal,https://github.com/apache/opendal,Apache-2.0,Apache OpenDAL <dev@opendal.apache.org>
opendal-core,https://github.com/apache/opendal,Apache-2.0,Apache OpenDAL <dev@opendal.apache.org>
opendal-http-transport-reqwest,https://github.com/apache/opendal,Apache-2.0,Apache OpenDAL <dev@opendal.apache.org>
opendal-layer-logging,https://github.com/apache/opendal,Apache-2.0,Apache OpenDAL <dev@opendal.apache.org>
opendal-service-webhdfs,https://github.com/apache/opendal,Apache-2.0,Apache OpenDAL <dev@opendal.apache.org>
openidconnect,https://github.com/ramosbugs/openidconnect-rs,MIT,David A. Ramos <ramos@cs.stanford.edu>
openssl,https://github.com/rust-openssl/rust-openssl,Apache-2.0,Steven Fackler <sfackler@gmail.com>
openssl-macros,https://github.com/sfackler/rust-openssl,MIT OR Apache-2.0,The openssl-macros Authors
openssl-probe,https://github.com/alexcrichton/openssl-probe,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
openssl-probe,https://github.com/rustls/openssl-probe,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
openssl-sys,https://github.com/rust-openssl/rust-openssl,MIT,"Alex Crichton <alex@alexcrichton.com>, Steven Fackler <sfackler@gmail.com>"
ordered-float,https://github.com/reem/rust-ordered-float,MIT,"Jonathan Reem <jonathan.reem@gmail.com>, Matt Brubeck <mbrubeck@limpet.net>"
ouroboros,https://github.com/someguynamedjosh/ouroboros,MIT OR Apache-2.0,Josh <someguynamedjosh@github.com>
ouroboros_macro,https://github.com/someguynamedjosh/ouroboros,MIT OR Apache-2.0,Josh <someguynamedjosh@github.com>
outref,https://github.com/Nugine/outref,MIT,The outref Authors
owo-colors,https://github.com/owo-colors/owo-colors,MIT,jam1garner <8260240+jam1garner@users.noreply.github.com>
p256,https://github.com/RustCrypto/elliptic-curves/tree/master/p256,Apache-2.0 OR MIT,RustCrypto Developers
p384,https://github.com/RustCrypto/elliptic-curves/tree/master/p384,Apache-2.0 OR MIT,"RustCrypto Developers, Frank Denis <github@pureftpd.org>"
pad,https://github.com/ogham/rust-pad,MIT,Ben S <ogham@bsago.me>
palette,https://github.com/Ogeon/palette,MIT OR Apache-2.0,Erik Hedvall <hello@erikhedvall.nu>
palette_derive,https://github.com/Ogeon/palette,MIT OR Apache-2.0,Erik Hedvall <hello@erikhedvall.nu>
palette_math,https://github.com/Ogeon/palette,MIT OR Apache-2.0,Erik Hedvall <hello@erikhedvall.nu>
parking,https://github.com/smol-rs/parking,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, The Rust Project Developers"
parking_lot,https://github.com/Amanieu/parking_lot,MIT OR Apache-2.0,Amanieu d'Antras <amanieu@gmail.com>
parking_lot_core,https://github.com/Amanieu/parking_lot,MIT OR Apache-2.0,Amanieu d'Antras <amanieu@gmail.com>
parquet,https://github.com/apache/arrow-rs,Apache-2.0,Apache Arrow <dev@arrow.apache.org>
parse-size,https://github.com/kennytm/parse-size,MIT,kennytm <kennytm@gmail.com>
pastey,https://github.com/as1100k/pastey,MIT OR Apache-2.0,"Aditya Kumar <git@adityais.dev>, David Tolnay <dtolnay@gmail.com>"
pbkdf2,https://github.com/RustCrypto/password-hashes/tree/master/pbkdf2,MIT OR Apache-2.0,RustCrypto Developers
peeking_take_while,https://github.com/fitzgen/peeking_take_while,MIT OR Apache-2.0,Nick Fitzgerald <fitzgen@gmail.com>
pem,https://github.com/jcreekmore/pem-rs,MIT,Jonathan Creekmore <jonathan@thecreekmores.org>
pem-rfc7468,https://github.com/RustCrypto/formats/tree/master/pem-rfc7468,Apache-2.0 OR MIT,RustCrypto Developers
percent-encoding,https://github.com/servo/rust-url,MIT OR Apache-2.0,The rust-url developers
pest,https://github.com/pest-parser/pest,MIT OR Apache-2.0,Dragoș Tiselice <dragostiselice@gmail.com>
pest_derive,https://github.com/pest-parser/pest,MIT OR Apache-2.0,Dragoș Tiselice <dragostiselice@gmail.com>
pest_generator,https://github.com/pest-parser/pest,MIT OR Apache-2.0,Dragoș Tiselice <dragostiselice@gmail.com>
pest_meta,https://github.com/pest-parser/pest,MIT OR Apache-2.0,Dragoș Tiselice <dragostiselice@gmail.com>
phf,https://github.com/rust-phf/rust-phf,MIT,Steven Fackler <sfackler@gmail.com>
phf_shared,https://github.com/rust-phf/rust-phf,MIT,Steven Fackler <sfackler@gmail.com>
pin-project,https://github.com/taiki-e/pin-project,Apache-2.0 OR MIT,The pin-project Authors
pin-project-internal,https://github.com/taiki-e/pin-project,Apache-2.0 OR MIT,The pin-project-internal Authors
pin-project-lite,https://github.com/taiki-e/pin-project-lite,Apache-2.0 OR MIT,The pin-project-lite Authors
pin-utils,https://github.com/rust-lang-nursery/pin-utils,MIT OR Apache-2.0,Josef Brandl <mail@josefbrandl.de>
piper,https://github.com/notgull/piper,MIT OR Apache-2.0,"Stjepan Glavina <stjepang@gmail.com>, John Nunley <dev@notgull.net>"
pkcs1,https://github.com/RustCrypto/formats/tree/master/pkcs1,Apache-2.0 OR MIT,RustCrypto Developers
pkcs8,https://github.com/RustCrypto/formats/tree/master/pkcs8,Apache-2.0 OR MIT,RustCrypto Developers
plain,https://github.com/randomites/plain,MIT OR Apache-2.0,jzr
platforms,https://github.com/RustSec/platforms-crate,Apache-2.0 OR MIT,Tony Arcieri <bascule@gmail.com>
polling,https://github.com/smol-rs/polling,Apache-2.0 OR MIT,"Stjepan Glavina <stjepang@gmail.com>, John Nunley <dev@notgull.net>"
poly1305,https://github.com/RustCrypto/universal-hashes,Apache-2.0 OR MIT,RustCrypto Developers
portable-atomic,https://github.com/taiki-e/portable-atomic,Apache-2.0 OR MIT,The portable-atomic Authors
portable-atomic-util,https://github.com/taiki-e/portable-atomic,Apache-2.0 OR MIT,The portable-atomic-util Authors
postgres-openssl,https://github.com/rust-postgres/rust-postgres,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
postgres-protocol,https://github.com/rust-postgres/rust-postgres,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
postgres-types,https://github.com/rust-postgres/rust-postgres,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
powerfmt,https://github.com/jhpratt/powerfmt,MIT OR Apache-2.0,Jacob Pratt <jacob@jhpratt.dev>
ppv-lite86,https://github.com/cryptocorrosion/cryptocorrosion,MIT OR Apache-2.0,The CryptoCorrosion Contributors
prefix-trie,https://github.com/tiborschneider/prefix-trie,MIT OR Apache-2.0,The prefix-trie Authors
prettydiff,https://github.com/romankoblov/prettydiff,MIT,Roman Koblov <penpen938@me.com>
prettyplease,https://github.com/dtolnay/prettyplease,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
prettytable-rs,https://github.com/phsym/prettytable-rs,BSD-3-Clause,Pierre-Henri Symoneaux
primeorder,https://github.com/RustCrypto/elliptic-curves/tree/master/primeorder,Apache-2.0 OR MIT,RustCrypto Developers
proc-macro-crate,https://github.com/bkchr/proc-macro-crate,MIT OR Apache-2.0,Bastian Köcher <git@kchr.de>
proc-macro-error-attr2,https://github.com/GnomedDev/proc-macro-error-2,MIT OR Apache-2.0,"CreepySkeleton <creepy-skeleton@yandex.ru>, GnomedDev <david2005thomas@gmail.com>"
proc-macro-error2,https://github.com/GnomedDev/proc-macro-error-2,MIT OR Apache-2.0,"CreepySkeleton <creepy-skeleton@yandex.ru>, GnomedDev <david2005thomas@gmail.com>"
proc-macro-hack,https://github.com/dtolnay/proc-macro-hack,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
proc-macro-nested,https://github.com/dtolnay/proc-macro-hack,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
proc-macro2,https://github.com/dtolnay/proc-macro2,MIT OR Apache-2.0,"David Tolnay <dtolnay@gmail.com>, Alex Crichton <alex@alexcrichton.com>"
proc-macro2-diagnostics,https://github.com/SergioBenitez/proc-macro2-diagnostics,MIT OR Apache-2.0,Sergio Benitez <sb@sergio.bz>
procfs,https://github.com/eminence/procfs,MIT OR Apache-2.0,Andrew Chin <achin@eminence32.net>
procfs-core,https://github.com/eminence/procfs,MIT OR Apache-2.0,Andrew Chin <achin@eminence32.net>
proptest,https://github.com/proptest-rs/proptest,MIT OR Apache-2.0,Jason Lingle
proptest-derive,https://github.com/proptest-rs/proptest,MIT OR Apache-2.0,Mazdak Farrokhzad <twingoow@gmail.com>
prost,https://github.com/tokio-rs/prost,Apache-2.0,"Dan Burkert <dan@danburkert.com>, Lucio Franco <luciofranco14@gmail.com>, Casper Meijn <casper@meijn.net>, Tokio Contributors <team@tokio.rs>"
prost-derive,https://github.com/tokio-rs/prost,Apache-2.0,"Dan Burkert <dan@danburkert.com>, Lucio Franco <luciofranco14@gmail.com>, Casper Meijn <casper@meijn.net>, Tokio Contributors <team@tokio.rs>"
prost-reflect,https://github.com/andrewhickman/prost-reflect,MIT OR Apache-2.0,Andrew Hickman <andrew.hickman1@sky.com>
prost-types,https://github.com/tokio-rs/prost,Apache-2.0,"Dan Burkert <dan@danburkert.com>, Lucio Franco <luciofranco14@gmail.com>, Casper Meijn <casper@meijn.net>, Tokio Contributors <team@tokio.rs>"
protobuf,https://github.com/stepancheg/rust-protobuf,MIT,Stepan Koltsov <stepan.koltsov@gmail.com>
protobuf-support,https://github.com/stepancheg/rust-protobuf,MIT,Stepan Koltsov <stepan.koltsov@gmail.com>
psl,https://github.com/addr-rs/psl,MIT OR Apache-2.0,rushmorem <rushmore@webenchanter.com>
psl-types,https://github.com/addr-rs/psl-types,MIT OR Apache-2.0,rushmorem <rushmore@webenchanter.com>
ptr_meta,https://github.com/djkoloski/ptr_meta,MIT,David Koloski <djkoloski@gmail.com>
ptr_meta_derive,https://github.com/djkoloski/ptr_meta,MIT,David Koloski <djkoloski@gmail.com>
publicsuffix,https://github.com/rushmorem/publicsuffix,MIT OR Apache-2.0,rushmorem <rushmore@webenchanter.com>
pulsar,https://github.com/streamnative/pulsar-rs,MIT OR Apache-2.0,"Colin Stearns <cstearns@developers.wyyerd.com>, Kevin Stenerson <kstenerson@developers.wyyerd.com>, Geoffroy Couprie <contact@geoffroycouprie.com>"
quad-rand,https://github.com/not-fl3/quad-rand,MIT,not-fl3 <not.fl3@gmail.com>
quanta,https://github.com/metrics-rs/quanta,MIT,Toby Lawrence <toby@nuclearfurnace.com>
quick-error,http://github.com/tailhook/quick-error,MIT OR Apache-2.0,"Paul Colomiets <paul@colomiets.name>, Colin Kiegel <kiegel@gmx.de>"
quick-junit,https://github.com/nextest-rs/quick-junit,Apache-2.0 OR MIT,The quick-junit Authors
quick-xml,https://github.com/tafia/quick-xml,MIT,The quick-xml Authors
quickcheck,https://github.com/BurntSushi/quickcheck,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
quinn,https://github.com/quinn-rs/quinn,MIT OR Apache-2.0,The quinn Authors
quinn-proto,https://github.com/quinn-rs/quinn,MIT OR Apache-2.0,The quinn-proto Authors
quinn-udp,https://github.com/quinn-rs/quinn,MIT OR Apache-2.0,The quinn-udp Authors
quote,https://github.com/dtolnay/quote,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
quoted_printable,https://github.com/staktrace/quoted-printable,0BSD,Kartikaya Gupta <kats@seldon.staktrace.com>
r-efi,https://github.com/r-efi/r-efi,MIT OR Apache-2.0 OR LGPL-2.1-or-later,The r-efi Authors
radium,https://github.com/bitvecto-rs/radium,MIT,"Nika Layzell <nika@thelayzells.com>, myrrlyn <self@myrrlyn.dev>"
radix_trie,https://github.com/michaelsproul/rust_radix_trie,MIT,Michael Sproul <micsproul@gmail.com>
rand,https://github.com/rust-random/rand,MIT OR Apache-2.0,"The Rand Project Developers, The Rust Project Developers"
rand_chacha,https://github.com/rust-random/rand,MIT OR Apache-2.0,"The Rand Project Developers, The Rust Project Developers, The CryptoCorrosion Contributors"
rand_core,https://github.com/rust-random/rand,MIT OR Apache-2.0,"The Rand Project Developers, The Rust Project Developers"
rand_core,https://github.com/rust-random/rand_core,MIT OR Apache-2.0,The Rand Project Developers
rand_distr,https://github.com/rust-random/rand_distr,MIT OR Apache-2.0,The Rand Project Developers
rand_pcg,https://github.com/rust-random/rngs,MIT OR Apache-2.0,The Rand Project Developers
rand_xorshift,https://github.com/rust-random/rngs,MIT OR Apache-2.0,"The Rand Project Developers, The Rust Project Developers"
rand_xoshiro,https://github.com/rust-random/rngs,MIT OR Apache-2.0,The Rand Project Developers
rapidhash,https://github.com/hoxxep/rapidhash,MIT OR Apache-2.0,Liam Gray <gmail@liamg.me>
ratatui,https://github.com/ratatui/ratatui,MIT,"Florian Dehau <work@fdehau.com>, The Ratatui Developers"
ratatui-core,https://github.com/ratatui/ratatui,MIT,"Florian Dehau <work@fdehau.com>, The Ratatui Developers"
ratatui-crossterm,https://github.com/ratatui/ratatui,MIT,"Florian Dehau <work@fdehau.com>, The Ratatui Developers"
ratatui-widgets,https://github.com/ratatui/ratatui,MIT,"Florian Dehau <work@fdehau.com>, The Ratatui Developers"
raw-cpuid,https://github.com/gz/rust-cpuid,MIT,Gerd Zellweger <mail@gerdzellweger.com>
rdkafka,https://github.com/fede1024/rust-rdkafka,MIT,Federico Giraud <giraud.federico@gmail.com>
rdkafka-sys,https://github.com/fede1024/rust-rdkafka,MIT,Federico Giraud <giraud.federico@gmail.com>
redis,https://github.com/redis-rs/redis-rs,BSD-3-Clause,The redis Authors
redox_syscall,https://gitlab.redox-os.org/redox-os/syscall,MIT,Jeremy Soller <jackpot51@gmail.com>
redox_users,https://gitlab.redox-os.org/redox-os/users,MIT,"Jose Narvaez <goyox86@gmail.com>, Wesley Hershberger <mggmugginsmc@gmail.com>"
ref-cast,https://github.com/dtolnay/ref-cast,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
ref-cast-impl,https://github.com/dtolnay/ref-cast,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
referencing,https://github.com/Stranger6667/jsonschema,MIT,Dmitry Dygalo <dmitry@dygalo.dev>
regex,https://github.com/rust-lang/regex,MIT OR Apache-2.0,"The Rust Project Developers, Andrew Gallant <jamslam@gmail.com>"
regex-automata,https://github.com/rust-lang/regex,MIT OR Apache-2.0,"The Rust Project Developers, Andrew Gallant <jamslam@gmail.com>"
regex-filtered,https://github.com/ua-parser/uap-rust,BSD-3-Clause,The regex-filtered Authors
regex-lite,https://github.com/rust-lang/regex,MIT OR Apache-2.0,"The Rust Project Developers, Andrew Gallant <jamslam@gmail.com>"
regex-syntax,https://github.com/rust-lang/regex/tree/master/regex-syntax,MIT OR Apache-2.0,"The Rust Project Developers, Andrew Gallant <jamslam@gmail.com>"
relative-path,https://github.com/udoprog/relative-path,MIT OR Apache-2.0,John-John Tedro <udoprog@tedro.se>
rend,https://github.com/djkoloski/rend,MIT,David Koloski <djkoloski@gmail.com>
reqwest,https://github.com/seanmonstar/reqwest,MIT OR Apache-2.0,Sean McArthur <sean@seanmonstar.com>
reqwest-middleware,https://github.com/TrueLayer/reqwest-middleware,MIT OR Apache-2.0,Rodrigo Gryzinski <rodrigo.gryzinski@truelayer.com>
reqwest-retry,https://github.com/TrueLayer/reqwest-middleware,MIT OR Apache-2.0,Rodrigo Gryzinski <rodrigo.gryzinski@truelayer.com>
resolv-conf,https://github.com/hickory-dns/resolv-conf,MIT OR Apache-2.0,The resolv-conf Authors
retry-policies,https://github.com/TrueLayer/retry-policies,MIT OR Apache-2.0,Luca Palmieri <lpalmieri@truelayer.com>
rfc6979,https://github.com/RustCrypto/signatures/tree/master/rfc6979,Apache-2.0 OR MIT,RustCrypto Developers
ring,https://github.com/briansmith/ring,Apache-2.0 AND ISC,The ring Authors
rkyv,https://github.com/rkyv/rkyv,MIT,David Koloski <djkoloski@gmail.com>
rkyv_derive,https://github.com/rkyv/rkyv,MIT,David Koloski <djkoloski@gmail.com>
rmp,https://github.com/3Hren/msgpack-rust,MIT,"Evgeny Safronov <division494@gmail.com>, Kornel <kornel@geekhood.net>"
rmp-serde,https://github.com/3Hren/msgpack-rust,MIT,Evgeny Safronov <division494@gmail.com>
rmpv,https://github.com/3Hren/msgpack-rust,MIT,Evgeny Safronov <division494@gmail.com>
roaring,https://github.com/RoaringBitmap/roaring-rs,MIT OR Apache-2.0,"Wim Looman <wim@nemo157.com>, Kerollmops <kero@meilisearch.com>"
roxmltree,https://github.com/RazrFalcon/roxmltree,MIT OR Apache-2.0,Yevhenii Reizner <razrfalcon@gmail.com>
rsa,https://github.com/RustCrypto/RSA,MIT OR Apache-2.0,"RustCrypto Developers, dignifiedquire <dignifiedquire@gmail.com>"
rumqttc,https://github.com/bytebeamio/rumqtt,Apache-2.0,tekjar <raviteja@bytebeam.io>
rust_decimal,https://github.com/paupino/rust-decimal,MIT,Paul Mason <paul@form1.co.nz>
rustc-hash,https://github.com/rust-lang/rustc-hash,Apache-2.0 OR MIT,The Rust Project Developers
rustc_version,https://github.com/djc/rustc-version-rs,MIT OR Apache-2.0,The rustc_version Authors
rustc_version_runtime,https://github.com/seppo0010/rustc-version-runtime-rs,MIT,Sebastian Waisbrot <seppo0010@gmail.com>
rustix,https://github.com/bytecodealliance/rustix,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,"Dan Gohman <dev@sunfishcode.online>, Jakub Konka <kubkon@jakubkonka.com>"
rustls,https://github.com/rustls/rustls,Apache-2.0 OR ISC OR MIT,The rustls Authors
rustls-native-certs,https://github.com/rustls/rustls-native-certs,Apache-2.0 OR ISC OR MIT,The rustls-native-certs Authors
rustls-pemfile,https://github.com/rustls/pemfile,Apache-2.0 OR ISC OR MIT,The rustls-pemfile Authors
rustls-pki-types,https://github.com/rustls/pki-types,MIT OR Apache-2.0,The rustls-pki-types Authors
rustls-platform-verifier,https://github.com/rustls/rustls-platform-verifier,MIT OR Apache-2.0,The rustls-platform-verifier Authors
rustls-platform-verifier-android,https://github.com/rustls/rustls-platform-verifier,MIT OR Apache-2.0,The rustls-platform-verifier-android Authors
rustls-webpki,https://github.com/rustls/webpki,ISC,The rustls-webpki Authors
rustversion,https://github.com/dtolnay/rustversion,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
rusty-fork,https://github.com/altsysrq/rusty-fork,MIT OR Apache-2.0,Jason Lingle
rustyline,https://github.com/kkawakam/rustyline,MIT,Katsu Kawakami <kkawa1570@gmail.com>
ryu,https://github.com/dtolnay/ryu,Apache-2.0 OR BSL-1.0,David Tolnay <dtolnay@gmail.com>
salsa20,https://github.com/RustCrypto/stream-ciphers,MIT OR Apache-2.0,RustCrypto Developers
same-file,https://github.com/BurntSushi/same-file,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
sasl2-sys,https://github.com/MaterializeInc/rust-sasl,Apache-2.0,"Materialize, Inc."
schannel,https://github.com/steffengy/schannel-rs,MIT,"Steven Fackler <sfackler@gmail.com>, Steffen Butzer <steffen.butzer@outlook.com>"
schemars,https://github.com/GREsau/schemars,MIT,Graham Esau <gesau@hotmail.co.uk>
scoped-tls,https://github.com/alexcrichton/scoped-tls,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
scopeguard,https://github.com/bluss/scopeguard,MIT OR Apache-2.0,bluss
seahash,https://gitlab.redox-os.org/redox-os/seahash,MIT,"ticki <ticki@users.noreply.github.com>, Tom Almeida <tom@tommoa.me>"
sec1,https://github.com/RustCrypto/formats/tree/master/sec1,Apache-2.0 OR MIT,RustCrypto Developers
secrecy,https://github.com/iqlusioninc/crates/tree/main/secrecy,Apache-2.0 OR MIT,Tony Arcieri <tony@iqlusion.io>
security-framework,https://github.com/kornelski/rust-security-framework,MIT OR Apache-2.0,"Steven Fackler <sfackler@gmail.com>, Kornel <kornel@geekhood.net>"
security-framework-sys,https://github.com/kornelski/rust-security-framework,MIT OR Apache-2.0,"Steven Fackler <sfackler@gmail.com>, Kornel <kornel@geekhood.net>"
semver,https://github.com/dtolnay/semver,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
seq-macro,https://github.com/dtolnay/seq-macro,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
serde,https://github.com/serde-rs/serde,MIT OR Apache-2.0,"Erick Tryzelaar <erick.tryzelaar@gmail.com>, David Tolnay <dtolnay@gmail.com>"
serde-saphyr,https://github.com/bourumir-wyngs/serde-saphyr,MIT OR Apache-2.0,The serde-saphyr Authors
serde-value,https://github.com/arcnmx/serde-value,MIT,arcnmx
serde_bytes,https://github.com/serde-rs/bytes,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
serde_core,https://github.com/serde-rs/serde,MIT OR Apache-2.0,"Erick Tryzelaar <erick.tryzelaar@gmail.com>, David Tolnay <dtolnay@gmail.com>"
serde_derive,https://github.com/serde-rs/serde,MIT OR Apache-2.0,"Erick Tryzelaar <erick.tryzelaar@gmail.com>, David Tolnay <dtolnay@gmail.com>"
serde_derive_internals,https://github.com/serde-rs/serde,MIT OR Apache-2.0,"Erick Tryzelaar <erick.tryzelaar@gmail.com>, David Tolnay <dtolnay@gmail.com>"
serde_json,https://github.com/serde-rs/json,MIT OR Apache-2.0,"Erick Tryzelaar <erick.tryzelaar@gmail.com>, David Tolnay <dtolnay@gmail.com>"
serde_nanos,https://github.com/caspervonb/serde_nanos,MIT OR Apache-2.0,Casper Beyer <caspervonb@pm.me>
serde_path_to_error,https://github.com/dtolnay/path-to-error,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
serde_plain,https://github.com/mitsuhiko/serde-plain,MIT OR Apache-2.0,Armin Ronacher <armin.ronacher@active-4.com>
serde_repr,https://github.com/dtolnay/serde-repr,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
serde_spanned,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The serde_spanned Authors
serde_urlencoded,https://github.com/nox/serde_urlencoded,MIT OR Apache-2.0,Anthony Ramine <n.oxyde@gmail.com>
serde_with,https://github.com/jonasbb/serde_with,MIT OR Apache-2.0,"Jonas Bushart, Marcin Kaźmierczak"
serde_with_macros,https://github.com/jonasbb/serde_with,MIT OR Apache-2.0,Jonas Bushart
serde_yaml,https://github.com/dtolnay/serde-yaml,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
serde_yaml_ng,https://github.com/acatton/serde-yaml-ng,MIT,Antoine Catton <devel@antoine.catton.fr>
sha1,https://github.com/RustCrypto/hashes,MIT OR Apache-2.0,RustCrypto Developers
sha2,https://github.com/RustCrypto/hashes,MIT OR Apache-2.0,RustCrypto Developers
sha3,https://github.com/RustCrypto/hashes,MIT OR Apache-2.0,RustCrypto Developers
sharded-slab,https://github.com/hawkw/sharded-slab,MIT,Eliza Weisman <eliza@buoyant.io>
signal-hook,https://github.com/vorner/signal-hook,Apache-2.0 OR MIT,"Michal 'vorner' Vaner <vorner@vorner.cz>, Thomas Himmelstoss <thimm@posteo.de>"
signal-hook-mio,https://github.com/vorner/signal-hook,MIT OR Apache-2.0,"Michal 'vorner' Vaner <vorner@vorner.cz>, Thomas Himmelstoss <thimm@posteo.de>"
signal-hook-registry,https://github.com/vorner/signal-hook,Apache-2.0 OR MIT,"Michal 'vorner' Vaner <vorner@vorner.cz>, Masaki Hara <ackie.h.gmai@gmail.com>"
signatory,https://github.com/iqlusioninc/crates/tree/main/signatory,Apache-2.0 OR MIT,Tony Arcieri <tony@iqlusion.io>
signature,https://github.com/RustCrypto/traits/tree/master/signature,Apache-2.0 OR MIT,RustCrypto Developers
simd-adler32,https://github.com/mcountryman/simd-adler32,MIT,Marvin Countryman <me@maar.vin>
simd_cesu8,https://github.com/seancroach/simd_cesu8,Apache-2.0 OR MIT,Sean C. Roach <me@seancroach.dev>
simdutf8,https://github.com/rusticstuff/simdutf8,MIT OR Apache-2.0,Hans Kratz <hans@appfour.com>
simpl,https://github.com/durch/simplerr,MIT,Drazen Urch <drazen@urch.eu>
siphasher,https://github.com/jedisct1/rust-siphash,MIT OR Apache-2.0,Frank Denis <github@pureftpd.org>
sketches-ddsketch,https://github.com/mheffner/rust-sketches-ddsketch,Apache-2.0,Mike Heffner <mikeh@fesnel.com>
slab,https://github.com/tokio-rs/slab,MIT,Carl Lerche <me@carllerche.com>
smallvec,https://github.com/servo/rust-smallvec,MIT OR Apache-2.0,The Servo Project Developers
smol,https://github.com/smol-rs/smol,Apache-2.0 OR MIT,Stjepan Glavina <stjepang@gmail.com>
smpl_jwt,https://github.com/durch/rust-jwt,MIT,Drazen Urch <github@drazenur.ch>
snafu,https://github.com/shepmaster/snafu,MIT OR Apache-2.0,Jake Goulding <jake.goulding@gmail.com>
snafu-derive,https://github.com/shepmaster/snafu,MIT OR Apache-2.0,Jake Goulding <jake.goulding@gmail.com>
snap,https://github.com/BurntSushi/rust-snappy,BSD-3-Clause,Andrew Gallant <jamslam@gmail.com>
socket2,https://github.com/rust-lang/socket2,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Thomas de Zeeuw <thomasdezeeuw@gmail.com>"
spin,https://github.com/mvdnes/spin-rs,MIT,"Mathijs van de Nes <git@mathijs.vd-nes.nl>, John Ericson <git@JohnEricson.me>"
spin,https://github.com/mvdnes/spin-rs,MIT,"Mathijs van de Nes <git@mathijs.vd-nes.nl>, John Ericson <git@JohnEricson.me>, Joshua Barretto <joshua.s.barretto@gmail.com>"
spinning_top,https://github.com/rust-osdev/spinning_top,MIT OR Apache-2.0,Philipp Oppermann <dev@phil-opp.com>
spki,https://github.com/RustCrypto/formats/tree/master/spki,Apache-2.0 OR MIT,RustCrypto Developers
sponge-cursor,https://github.com/RustCrypto/utils,MIT OR Apache-2.0,RustCrypto Developers
sqlx,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
sqlx-core,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
sqlx-macros,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
sqlx-macros-core,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
sqlx-mysql,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
sqlx-postgres,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
sqlx-sqlite,https://github.com/launchbadge/sqlx,MIT OR Apache-2.0,"Ryan Leckey <leckey.ryan@gmail.com>, Austin Bonander <austin.bonander@gmail.com>, Chloe Ross <orangesnowfox@gmail.com>, Daniel Akhterov <akhterovd@gmail.com>"
stable_deref_trait,https://github.com/storyyeller/stable_deref_trait,MIT OR Apache-2.0,Robert Grosse <n210241048576@gmail.com>
static_assertions,https://github.com/nvzqz/static-assertions-rs,MIT OR Apache-2.0,Nikolai Vazquez
stream-cancel,https://github.com/jonhoo/stream-cancel,MIT OR Apache-2.0,Jon Gjengset <jon@thesquareplanet.com>
stringprep,https://github.com/sfackler/rust-stringprep,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
strip-ansi-escapes,https://github.com/luser/strip-ansi-escapes,Apache-2.0 OR MIT,Ted Mielczarek <ted@mielczarek.org>
strsim,https://github.com/rapidfuzz/strsim-rs,MIT,"Danny Guo <danny@dannyguo.com>, maxbachmann <oss@maxbachmann.de>"
strum,https://github.com/Peternator7/strum,MIT,Peter Glotfelty <peter.glotfelty@microsoft.com>
strum_macros,https://github.com/Peternator7/strum,MIT,Peter Glotfelty <peter.glotfelty@microsoft.com>
subtle,https://github.com/dalek-cryptography/subtle,BSD-3-Clause,"Isis Lovecruft <isis@patternsinthevoid.net>, Henry de Valence <hdevalence@hdevalence.ca>"
supports-color,https://github.com/zkat/supports-color,Apache-2.0,Kat Marchán <kzm@zkat.tech>
syn,https://github.com/dtolnay/syn,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
sync_wrapper,https://github.com/Actyx/sync_wrapper,Apache-2.0,Actyx AG <developer@actyx.io>
synstructure,https://github.com/mystor/synstructure,MIT,Nika Layzell <nika@thelayzells.com>
sysinfo,https://github.com/GuillaumeGomez/sysinfo,MIT,Guillaume Gomez <guillaume1.gomez@gmail.com>
syslog,https://github.com/Geal/rust-syslog,MIT,contact@geoffroycouprie.com
syslog_loose,https://github.com/FungusHumungus/syslog-loose,MIT,Stephen Wakely <fungus.humungus@gmail.com>
system-configuration,https://github.com/mullvad/system-configuration-rs,MIT OR Apache-2.0,Mullvad VPN
system-configuration-sys,https://github.com/mullvad/system-configuration-rs,MIT OR Apache-2.0,Mullvad VPN
tagptr,https://github.com/oliver-giersch/tagptr,MIT OR Apache-2.0,Oliver Giersch
take_mut,https://github.com/Sgeo/take_mut,MIT,Sgeo <sgeoster@gmail.com>
tap,https://github.com/myrrlyn/tap,MIT,"Elliott Linder <elliott.darfink@gmail.com>, myrrlyn <self@myrrlyn.dev>"
tcp-stream,https://github.com/amqp-rs/tcp-stream,BSD-2-Clause,Marc-Antoine Perennou <Marc-Antoine@Perennou.com>
tempfile,https://github.com/Stebalien/tempfile,MIT OR Apache-2.0,"Steven Allen <steven@stebalien.com>, The Rust Project Developers, Ashley Mannix <ashleymannix@live.com.au>, Jason White <me@jasonwhite.io>"
term,https://github.com/Stebalien/term,MIT OR Apache-2.0,"The Rust Project Developers, Steven Allen"
termcolor,https://github.com/BurntSushi/termcolor,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
terminal_size,https://github.com/eminence/terminal-size,MIT OR Apache-2.0,Andrew Chin <achin@eminence32.net>
thiserror,https://github.com/dtolnay/thiserror,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
thiserror-impl,https://github.com/dtolnay/thiserror,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
thread_local,https://github.com/Amanieu/thread_local-rs,MIT OR Apache-2.0,Amanieu d'Antras <amanieu@gmail.com>
tikv-jemalloc-sys,https://github.com/tikv/jemallocator,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Gonzalo Brito Gadeschi <gonzalobg88@gmail.com>, The TiKV Project Developers"
tikv-jemallocator,https://github.com/tikv/jemallocator,MIT OR Apache-2.0,"Alex Crichton <alex@alexcrichton.com>, Gonzalo Brito Gadeschi <gonzalobg88@gmail.com>, Simon Sapin <simon.sapin@exyr.org>, Steven Fackler <sfackler@gmail.com>, The TiKV Project Developers"
time,https://github.com/time-rs/time,MIT OR Apache-2.0,"Jacob Pratt <open-source@jhpratt.dev>, Time contributors"
time-core,https://github.com/time-rs/time,MIT OR Apache-2.0,"Jacob Pratt <open-source@jhpratt.dev>, Time contributors"
time-macros,https://github.com/time-rs/time,MIT OR Apache-2.0,"Jacob Pratt <open-source@jhpratt.dev>, Time contributors"
tiny-keccak,https://github.com/debris/tiny-keccak,CC0-1.0,debris <marek.kotewicz@gmail.com>
tinystr,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
tinyvec,https://github.com/Lokathor/tinyvec,Zlib OR Apache-2.0 OR MIT,Lokathor <zefria@gmail.com>
tinyvec_macros,https://github.com/Soveu/tinyvec_macros,MIT OR Apache-2.0 OR Zlib,Soveu <marx.tomasz@gmail.com>
tokio,https://github.com/tokio-rs/tokio,MIT,Tokio Contributors <team@tokio.rs>
tokio-io-timeout,https://github.com/sfackler/tokio-io-timeout,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
tokio-macros,https://github.com/tokio-rs/tokio,MIT,Tokio Contributors <team@tokio.rs>
tokio-native-tls,https://github.com/tokio-rs/tls,MIT,Tokio Contributors <team@tokio.rs>
tokio-openssl,https://github.com/tokio-rs/tokio-openssl,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
tokio-postgres,https://github.com/rust-postgres/rust-postgres,MIT OR Apache-2.0,Steven Fackler <sfackler@gmail.com>
tokio-retry,https://github.com/srijs/rust-tokio-retry,MIT,Sam Rijs <srijs@airpost.net>
tokio-rustls,https://github.com/rustls/tokio-rustls,MIT OR Apache-2.0,The tokio-rustls Authors
tokio-stream,https://github.com/tokio-rs/tokio,MIT,Tokio Contributors <team@tokio.rs>
tokio-tungstenite,https://github.com/snapview/tokio-tungstenite,MIT,"Daniel Abramov <dabramov@snapview.de>, Alexey Galakhov <agalakhov@snapview.de>"
tokio-util,https://github.com/tokio-rs/tokio,MIT,Tokio Contributors <team@tokio.rs>
tokio-websockets,https://github.com/Gelbpunkt/tokio-websockets,MIT,The tokio-websockets Authors
toml,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The toml Authors
toml_datetime,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The toml_datetime Authors
toml_edit,https://github.com/toml-rs/toml,MIT OR Apache-2.0,"Andronik Ordian <write@reusable.software>, Ed Page <eopage@gmail.com>"
toml_edit,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The toml_edit Authors
toml_parser,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The toml_parser Authors
toml_write,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The toml_write Authors
toml_writer,https://github.com/toml-rs/toml,MIT OR Apache-2.0,The toml_writer Authors
tonic,https://github.com/hyperium/tonic,MIT,Lucio Franco <luciofranco14@gmail.com>
tonic-health,https://github.com/hyperium/tonic,MIT,James Nugent <james@jen20.com>
tonic-prost,https://github.com/hyperium/tonic,MIT,Lucio Franco <luciofranco14@gmail.com>
tonic-reflection,https://github.com/hyperium/tonic,MIT,"James Nugent <james@jen20.com>, Samani G. Gikandi <samani@gojulas.com>"
tonic-types,https://github.com/hyperium/tonic,MIT,"Lucio Franco <luciofranco14@gmail.com>, Rafael Lemos <flemos.rafael.dev@gmail.com>"
tower,https://github.com/tower-rs/tower,MIT,Tower Maintainers <team@tower-rs.com>
tower-http,https://github.com/tower-rs/tower-http,MIT,Tower Maintainers <team@tower-rs.com>
tower-layer,https://github.com/tower-rs/tower,MIT,Tower Maintainers <team@tower-rs.com>
tower-service,https://github.com/tower-rs/tower,MIT,Tower Maintainers <team@tower-rs.com>
tracing,https://github.com/tokio-rs/tracing,MIT,"Eliza Weisman <eliza@buoyant.io>, Tokio Contributors <team@tokio.rs>"
tracing-attributes,https://github.com/tokio-rs/tracing,MIT,"Tokio Contributors <team@tokio.rs>, Eliza Weisman <eliza@buoyant.io>, David Barsky <dbarsky@amazon.com>"
tracing-core,https://github.com/tokio-rs/tracing,MIT,Tokio Contributors <team@tokio.rs>
tracing-futures,https://github.com/tokio-rs/tracing,MIT,"Eliza Weisman <eliza@buoyant.io>, Tokio Contributors <team@tokio.rs>"
tracing-log,https://github.com/tokio-rs/tracing,MIT,Tokio Contributors <team@tokio.rs>
tracing-serde,https://github.com/tokio-rs/tracing,MIT,Tokio Contributors <team@tokio.rs>
tracing-subscriber,https://github.com/tokio-rs/tracing,MIT,"Eliza Weisman <eliza@buoyant.io>, David Barsky <me@davidbarsky.com>, Tokio Contributors <team@tokio.rs>"
tracing-tower,https://github.com/tokio-rs/tracing,MIT,Eliza Weisman <eliza@buoyant.io>
triomphe,https://github.com/Manishearth/triomphe,MIT OR Apache-2.0,"Manish Goregaokar <manishsmail@gmail.com>, The Servo Project Developers"
try-lock,https://github.com/seanmonstar/try-lock,MIT,Sean McArthur <sean@seanmonstar.com>
tryhard,https://github.com/EmbarkStudios/tryhard,MIT OR Apache-2.0,Embark <opensource@embark-studios.com>
tungstenite,https://github.com/snapview/tungstenite-rs,MIT OR Apache-2.0,"Alexey Galakhov, Daniel Abramov"
twox-hash,https://github.com/shepmaster/twox-hash,MIT,Jake Goulding <jake.goulding@gmail.com>
typed-builder,https://github.com/idanarye/rust-typed-builder,MIT OR Apache-2.0,"IdanArye <idanarye@gmail.com>, Chris Morgan <me@chrismorgan.info>"
typed-builder-macro,https://github.com/idanarye/rust-typed-builder,MIT OR Apache-2.0,"IdanArye <idanarye@gmail.com>, Chris Morgan <me@chrismorgan.info>"
typenum,https://github.com/paholg/typenum,MIT OR Apache-2.0,The typenum Authors
typespec,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
typespec_client_core,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
typespec_macros,https://github.com/azure/azure-sdk-for-rust,MIT,Microsoft
typetag,https://github.com/dtolnay/typetag,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
typetag-impl,https://github.com/dtolnay/typetag,MIT OR Apache-2.0,David Tolnay <dtolnay@gmail.com>
ua-parser,https://github.com/ua-parser/uap-rust,Apache-2.0,The ua-parser Authors
ucd-trie,https://github.com/BurntSushi/ucd-generate,MIT OR Apache-2.0,Andrew Gallant <jamslam@gmail.com>
unarray,https://github.com/cameron1024/unarray,MIT OR Apache-2.0,The unarray Authors
unicase,https://github.com/seanmonstar/unicase,MIT OR Apache-2.0,Sean McArthur <sean@seanmonstar.com>
unicode-bidi,https://github.com/servo/unicode-bidi,MIT OR Apache-2.0,The Servo Project Developers
unicode-general-category,https://github.com/yeslogic/unicode-general-category,Apache-2.0,YesLogic Pty. Ltd. <info@yeslogic.com>
unicode-ident,https://github.com/dtolnay/unicode-ident,(MIT OR Apache-2.0) AND Unicode-DFS-2016,David Tolnay <dtolnay@gmail.com>
unicode-normalization,https://github.com/unicode-rs/unicode-normalization,MIT OR Apache-2.0,"kwantam <kwantam@gmail.com>, Manish Goregaokar <manishsmail@gmail.com>"
unicode-segmentation,https://github.com/unicode-rs/unicode-segmentation,MIT OR Apache-2.0,"kwantam <kwantam@gmail.com>, Manish Goregaokar <manishsmail@gmail.com>"
unicode-truncate,https://github.com/Aetf/unicode-truncate,MIT OR Apache-2.0,Aetf <aetf@unlimitedcodeworks.xyz>
unicode-width,https://github.com/unicode-rs/unicode-width,MIT OR Apache-2.0,"kwantam <kwantam@gmail.com>, Manish Goregaokar <manishsmail@gmail.com>"
unicode-xid,https://github.com/unicode-rs/unicode-xid,MIT OR Apache-2.0,"erick.tryzelaar <erick.tryzelaar@gmail.com>, kwantam <kwantam@gmail.com>, Manish Goregaokar <manishsmail@gmail.com>"
unit-prefix,https://codeberg.org/commons-rs/unit-prefix,MIT,"Fabio Valentini <decathorpe@gmail.com>, Benjamin Sago <ogham@bsago.me>"
universal-hash,https://github.com/RustCrypto/traits,MIT OR Apache-2.0,RustCrypto Developers
unsafe-libyaml,https://github.com/dtolnay/unsafe-libyaml,MIT,David Tolnay <dtolnay@gmail.com>
untrusted,https://github.com/briansmith/untrusted,ISC,Brian Smith <brian@briansmith.org>
uom,https://github.com/iliekturtles/uom,Apache-2.0 OR MIT,Mike Boutin <mike.boutin@gmail.com>
url,https://github.com/servo/rust-url,MIT OR Apache-2.0,The rust-url developers
urlencoding,https://github.com/kornelski/rust_urlencoding,MIT,"Kornel <kornel@geekhood.net>, Bertram Truong <b@bertramtruong.com>"
utf-8,https://github.com/SimonSapin/rust-utf8,MIT OR Apache-2.0,Simon Sapin <simon.sapin@exyr.org>
utf16_iter,https://github.com/hsivonen/utf16_iter,Apache-2.0 OR MIT,Henri Sivonen <hsivonen@hsivonen.fi>
utf8-width,https://github.com/magiclen/utf8-width,MIT,Magic Len <len@magiclen.org>
utf8_iter,https://github.com/hsivonen/utf8_iter,Apache-2.0 OR MIT,Henri Sivonen <hsivonen@hsivonen.fi>
utf8parse,https://github.com/alacritty/vte,Apache-2.0 OR MIT,"Joe Wilm <joe@jwilm.com>, Christian Duerr <contact@christianduerr.com>"
uuid,https://github.com/uuid-rs/uuid,Apache-2.0 OR MIT,"Ashley Mannix<ashleymannix@live.com.au>, Dylan DPC<dylan.dpc@gmail.com>, Hunar Roop Kahlon<hunar.roop@gmail.com>"
uuid-simd,https://github.com/Nugine/simd,MIT,The uuid-simd Authors
valuable,https://github.com/tokio-rs/valuable,MIT,The valuable Authors
vrl,https://github.com/vectordotdev/vrl,MPL-2.0,Vector Contributors <vector@datadoghq.com>
vsimd,https://github.com/Nugine/simd,MIT,The vsimd Authors
vte,https://github.com/alacritty/vte,Apache-2.0 OR MIT,"Joe Wilm <joe@jwilm.com>, Christian Duerr <contact@christianduerr.com>"
wait-timeout,https://github.com/alexcrichton/wait-timeout,MIT OR Apache-2.0,Alex Crichton <alex@alexcrichton.com>
walkdir,https://github.com/BurntSushi/walkdir,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
want,https://github.com/seanmonstar/want,MIT,Sean McArthur <sean@seanmonstar.com>
warp,https://github.com/seanmonstar/warp,MIT,Sean McArthur <sean@seanmonstar.com>
wasi,https://github.com/bytecodealliance/wasi,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,The Cranelift Project Developers
wasi,https://github.com/bytecodealliance/wasi-rs,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,The wasi Authors
wasip2,https://github.com/bytecodealliance/wasi-rs,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,The wasip2 Authors
wasip3,https://github.com/bytecodealliance/wasi-rs,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,The wasip3 Authors
wasite,https://github.com/ardaku/wasite,Apache-2.0 OR BSL-1.0 OR MIT,The wasite Authors
wasm-bindgen,https://github.com/wasm-bindgen/wasm-bindgen,MIT OR Apache-2.0,The wasm-bindgen Developers
wasm-bindgen-futures,https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/futures,MIT OR Apache-2.0,The wasm-bindgen Developers
wasm-bindgen-macro,https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro,MIT OR Apache-2.0,The wasm-bindgen Developers
wasm-bindgen-macro-support,https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro-support,MIT OR Apache-2.0,The wasm-bindgen Developers
wasm-bindgen-shared,https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/shared,MIT OR Apache-2.0,The wasm-bindgen Developers
wasm-encoder,https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wasm-encoder,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Nick Fitzgerald <fitzgen@gmail.com>
wasm-metadata,https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wasm-metadata,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,The wasm-metadata Authors
wasm-streams,https://github.com/MattiasBuelens/wasm-streams,MIT OR Apache-2.0,Mattias Buelens <mattias@buelens.com>
wasmparser,https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wasmparser,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Yury Delendik <ydelendik@mozilla.com>
wasmtimer,https://github.com/whizsid/wasmtimer-rs,MIT,"WhizSid <whizsid@aol.com>, Pierre Krieger <pierre.krieger1708@gmail.com>"
web-sys,https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/web-sys,MIT OR Apache-2.0,The wasm-bindgen Developers
web-time,https://github.com/daxpedda/web-time,MIT OR Apache-2.0,The web-time Authors
webbrowser,https://github.com/amodm/webbrowser-rs,MIT OR Apache-2.0,Amod Malviya @amodm
webpki-root-certs,https://github.com/rustls/webpki-roots,CDLA-Permissive-2.0,The webpki-root-certs Authors
webpki-roots,https://github.com/rustls/webpki-roots,CDLA-Permissive-2.0,The webpki-roots Authors
webpki-roots,https://github.com/rustls/webpki-roots,MPL-2.0,The webpki-roots Authors
whoami,https://github.com/ardaku/whoami,Apache-2.0 OR BSL-1.0 OR MIT,The whoami Authors
widestring,https://github.com/starkat99/widestring-rs,MIT OR Apache-2.0,Kathryn Long <squeeself@gmail.com>
widestring,https://github.com/starkat99/widestring-rs,MIT OR Apache-2.0,The widestring Authors
winapi,https://github.com/retep998/winapi-rs,MIT OR Apache-2.0,Peter Atashian <retep998@gmail.com>
winapi-i686-pc-windows-gnu,https://github.com/retep998/winapi-rs,MIT OR Apache-2.0,Peter Atashian <retep998@gmail.com>
winapi-util,https://github.com/BurntSushi/winapi-util,Unlicense OR MIT,Andrew Gallant <jamslam@gmail.com>
winapi-x86_64-pc-windows-gnu,https://github.com/retep998/winapi-rs,MIT OR Apache-2.0,Peter Atashian <retep998@gmail.com>
windows,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-collections,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-collections Authors
windows-core,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-future,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-future Authors
windows-implement,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-implement,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-implement Authors
windows-interface,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-interface,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-interface Authors
windows-link,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-link,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-link Authors
windows-numerics,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-numerics Authors
windows-result,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-service,https://github.com/mullvad/windows-service-rs,MIT OR Apache-2.0,Mullvad VPN
windows-strings,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-sys,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-sys,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,The windows-sys Authors
windows-targets,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows-threading,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_aarch64_gnullvm,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_aarch64_msvc,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_i686_gnu,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_i686_gnullvm,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_i686_msvc,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_x86_64_gnu,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_x86_64_gnullvm,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
windows_x86_64_msvc,https://github.com/microsoft/windows-rs,MIT OR Apache-2.0,Microsoft
winnow,https://github.com/winnow-rs/winnow,MIT,The winnow Authors
winreg,https://github.com/gentoo90/winreg-rs,MIT,Igor Shaula <gentoo90@gmail.com>
wit-bindgen,https://github.com/bytecodealliance/wit-bindgen,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Alex Crichton <alex@alexcrichton.com>
wit-bindgen-core,https://github.com/bytecodealliance/wit-bindgen,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Alex Crichton <alex@alexcrichton.com>
wit-bindgen-rust,https://github.com/bytecodealliance/wit-bindgen,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Alex Crichton <alex@alexcrichton.com>
wit-bindgen-rust-macro,https://github.com/bytecodealliance/wit-bindgen,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Alex Crichton <alex@alexcrichton.com>
wit-component,https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wit-component,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Peter Huene <peter@huene.dev>
wit-parser,https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wit-parser,Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT,Alex Crichton <alex@alexcrichton.com>
woothee,https://github.com/woothee/woothee-rust,Apache-2.0,hhatto <hhatto.jp@gmail.com>
write16,https://github.com/hsivonen/write16,Apache-2.0 OR MIT,The write16 Authors
writeable,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
wyz,https://github.com/myrrlyn/wyz,MIT,myrrlyn <self@myrrlyn.dev>
xmlparser,https://github.com/RazrFalcon/xmlparser,MIT OR Apache-2.0,Yevhenii Reizner <razrfalcon@gmail.com>
xxhash-rust,https://github.com/DoumanAsh/xxhash-rust,BSL-1.0,Douman <douman@gmx.se>
yansi,https://github.com/SergioBenitez/yansi,MIT OR Apache-2.0,Sergio Benitez <sb@sergio.bz>
yoke,https://github.com/unicode-org/icu4x,Unicode-3.0,Manish Goregaokar <manishsmail@gmail.com>
yoke-derive,https://github.com/unicode-org/icu4x,Unicode-3.0,Manish Goregaokar <manishsmail@gmail.com>
zerocopy,https://github.com/google/zerocopy,BSD-2-Clause OR Apache-2.0 OR MIT,Joshua Liebow-Feeser <joshlf@google.com>
zerocopy-derive,https://github.com/google/zerocopy,BSD-2-Clause OR Apache-2.0 OR MIT,Joshua Liebow-Feeser <joshlf@google.com>
zerofrom,https://github.com/unicode-org/icu4x,Unicode-3.0,Manish Goregaokar <manishsmail@gmail.com>
zerofrom-derive,https://github.com/unicode-org/icu4x,Unicode-3.0,Manish Goregaokar <manishsmail@gmail.com>
zeroize,https://github.com/RustCrypto/utils/tree/master/zeroize,Apache-2.0 OR MIT,The RustCrypto Project Developers
zerovec,https://github.com/unicode-org/icu4x,Unicode-3.0,The ICU4X Project Developers
zerovec-derive,https://github.com/unicode-org/icu4x,Unicode-3.0,Manish Goregaokar <manishsmail@gmail.com>
zlib-rs,https://github.com/trifectatechfoundation/zlib-rs,Zlib,The zlib-rs Authors
zmij,https://github.com/dtolnay/zmij,MIT,David Tolnay <dtolnay@gmail.com>
zstd,https://github.com/gyscos/zstd-rs,MIT,Alexandre Bury <alexandre.bury@gmail.com>
zstd-safe,https://github.com/gyscos/zstd-rs,MIT OR Apache-2.0,Alexandre Bury <alexandre.bury@gmail.com>
zstd-sys,https://github.com/gyscos/zstd-rs,MIT OR Apache-2.0,Alexandre Bury <alexandre.bury@gmail.com>

```

## legal/NOTICE

```text
Unless explicitly stated otherwise all files in this repository are licensed under the Mozilla
Public License, version 2.0 (MPL-2.0).

This product includes software developed at Datadog (https://www.datadoghq.com/)

   Copyright (c) 2020 Vector Authors <vector@datadoghq.com>

   ADDITIONAL NOTICES FOR DISTRIBUTIONS IN OBJECT FORM

   1. This product includes software developed by the OpenSSL Project for
      use in the OpenSSL Toolkit (https://www.openssl.org/).

   2. This product includes cryptographic software written by Eric Young
      (eay@cryptsoft.com).

   ADDITIONAL NOTICES FOR DISTRIBUTIONS IN OBJECT FORM FOR WINDOWS

   1. This product includes software written by Tim Hudson
      (tjh@cryptsoft.com).

```

## legal/lib/dnsmsg-parser/LICENSE

```text
MIT License

Copyright (c) 2021, Bluecat Networks, Inc.

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.

```

## legal/lib/file-source/LICENSE

```text
The MIT License

Original work Copyright (c) 2016, Postmates, Inc.
Modified work Copyright (c) 2015, Mark Story <mark@mark-story.com>

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.

```

## legal/lib/file-source-common/LICENSE

```text
The MIT License

Original work Copyright (c) 2016, Postmates, Inc.
Modified work Copyright (c) 2015, Mark Story <mark@mark-story.com>

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.

```

## legal/lib/k8s-e2e-tests/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/k8s-test-framework/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/loki-logproto/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/loki-logproto/proto/LICENSE_APACHE2

```text

                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

```

## legal/lib/prometheus-parser/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/tracing-limit/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-api-client/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-common/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-config/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-config-common/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-config-common/src/schema/LICENSE-schemars

```text
MIT License

Copyright (c) 2019 Graham Esau

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

```

## legal/lib/vector-config-macros/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-core/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-lookup/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-vrl/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-vrl/cli/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-vrl/enrichment/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/lib/vector-vrl/tests/LICENSE

```text
Mozilla Public License, version 2.0

1. Definitions

1.1. "Contributor"

     means each individual or legal entity that creates, contributes to the
     creation of, or owns Covered Software.

1.2. "Contributor Version"

     means the combination of the Contributions of others (if any) used by a
     Contributor and that particular Contributor's Contribution.

1.3. "Contribution"

     means Covered Software of a particular Contributor.

1.4. "Covered Software"

     means Source Code Form to which the initial Contributor has attached the
     notice in Exhibit A, the Executable Form of such Source Code Form, and
     Modifications of such Source Code Form, in each case including portions
     thereof.

1.5. "Incompatible With Secondary Licenses"
     means

     a. that the initial Contributor has attached the notice described in
        Exhibit B to the Covered Software; or

     b. that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the terms of
        a Secondary License.

1.6. "Executable Form"

     means any form of the work other than Source Code Form.

1.7. "Larger Work"

     means a work that combines Covered Software with other material, in a
     separate file or files, that is not Covered Software.

1.8. "License"

     means this document.

1.9. "Licensable"

     means having the right to grant, to the maximum extent possible, whether
     at the time of the initial grant or subsequently, any and all of the
     rights conveyed by this License.

1.10. "Modifications"

     means any of the following:

     a. any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered Software; or

     b. any new file in Source Code Form that contains any Covered Software.

1.11. "Patent Claims" of a Contributor

      means any patent claim(s), including without limitation, method,
      process, and apparatus claims, in any patent Licensable by such
      Contributor that would be infringed, but for the grant of the License,
      by the making, using, selling, offering for sale, having made, import,
      or transfer of either its Contributions or its Contributor Version.

1.12. "Secondary License"

      means either the GNU General Public License, Version 2.0, the GNU Lesser
      General Public License, Version 2.1, the GNU Affero General Public
      License, Version 3.0, or any later versions of those licenses.

1.13. "Source Code Form"

      means the form of the work preferred for making modifications.

1.14. "You" (or "Your")

      means an individual or a legal entity exercising rights under this
      License. For legal entities, "You" includes any entity that controls, is
      controlled by, or is under common control with You. For purposes of this
      definition, "control" means (a) the power, direct or indirect, to cause
      the direction or management of such entity, whether by contract or
      otherwise, or (b) ownership of more than fifty percent (50%) of the
      outstanding shares or beneficial ownership of such entity.


2. License Grants and Conditions

2.1. Grants

     Each Contributor hereby grants You a world-wide, royalty-free,
     non-exclusive license:

     a. under intellectual property rights (other than patent or trademark)
        Licensable by such Contributor to use, reproduce, make available,
        modify, display, perform, distribute, and otherwise exploit its
        Contributions, either on an unmodified basis, with Modifications, or
        as part of a Larger Work; and

     b. under Patent Claims of such Contributor to make, use, sell, offer for
        sale, have made, import, and otherwise transfer either its
        Contributions or its Contributor Version.

2.2. Effective Date

     The licenses granted in Section 2.1 with respect to any Contribution
     become effective for each Contribution on the date the Contributor first
     distributes such Contribution.

2.3. Limitations on Grant Scope

     The licenses granted in this Section 2 are the only rights granted under
     this License. No additional rights or licenses will be implied from the
     distribution or licensing of Covered Software under this License.
     Notwithstanding Section 2.1(b) above, no patent license is granted by a
     Contributor:

     a. for any code that a Contributor has removed from Covered Software; or

     b. for infringements caused by: (i) Your and any other third party's
        modifications of Covered Software, or (ii) the combination of its
        Contributions with other software (except as part of its Contributor
        Version); or

     c. under Patent Claims infringed by Covered Software in the absence of
        its Contributions.

     This License does not grant any rights in the trademarks, service marks,
     or logos of any Contributor (except as may be necessary to comply with
     the notice requirements in Section 3.4).

2.4. Subsequent Licenses

     No Contributor makes additional grants as a result of Your choice to
     distribute the Covered Software under a subsequent version of this
     License (see Section 10.2) or under the terms of a Secondary License (if
     permitted under the terms of Section 3.3).

2.5. Representation

     Each Contributor represents that the Contributor believes its
     Contributions are its original creation(s) or it has sufficient rights to
     grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

     This License is not intended to limit any rights You have under
     applicable copyright doctrines of fair use, fair dealing, or other
     equivalents.

2.7. Conditions

     Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in
     Section 2.1.


3. Responsibilities

3.1. Distribution of Source Form

     All distribution of Covered Software in Source Code Form, including any
     Modifications that You create or to which You contribute, must be under
     the terms of this License. You must inform recipients that the Source
     Code Form of the Covered Software is governed by the terms of this
     License, and how they can obtain a copy of this License. You may not
     attempt to alter or restrict the recipients' rights in the Source Code
     Form.

3.2. Distribution of Executable Form

     If You distribute Covered Software in Executable Form then:

     a. such Covered Software must also be made available in Source Code Form,
        as described in Section 3.1, and You must inform recipients of the
        Executable Form how they can obtain a copy of such Source Code Form by
        reasonable means in a timely manner, at a charge no more than the cost
        of distribution to the recipient; and

     b. You may distribute such Executable Form under the terms of this
        License, or sublicense it under different terms, provided that the
        license for the Executable Form does not attempt to limit or alter the
        recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

     You may create and distribute a Larger Work under terms of Your choice,
     provided that You also comply with the requirements of this License for
     the Covered Software. If the Larger Work is a combination of Covered
     Software with a work governed by one or more Secondary Licenses, and the
     Covered Software is not Incompatible With Secondary Licenses, this
     License permits You to additionally distribute such Covered Software
     under the terms of such Secondary License(s), so that the recipient of
     the Larger Work may, at their option, further distribute the Covered
     Software under the terms of either this License or such Secondary
     License(s).

3.4. Notices

     You may not remove or alter the substance of any license notices
     (including copyright notices, patent notices, disclaimers of warranty, or
     limitations of liability) contained within the Source Code Form of the
     Covered Software, except that You may alter any license notices to the
     extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

     You may choose to offer, and to charge a fee for, warranty, support,
     indemnity or liability obligations to one or more recipients of Covered
     Software. However, You may do so only on Your own behalf, and not on
     behalf of any Contributor. You must make it absolutely clear that any
     such warranty, support, indemnity, or liability obligation is offered by
     You alone, and You hereby agree to indemnify every Contributor for any
     liability incurred by such Contributor as a result of warranty, support,
     indemnity or liability terms You offer. You may include additional
     disclaimers of warranty and limitations of liability specific to any
     jurisdiction.

4. Inability to Comply Due to Statute or Regulation

   If it is impossible for You to comply with any of the terms of this License
   with respect to some or all of the Covered Software due to statute,
   judicial order, or regulation then You must: (a) comply with the terms of
   this License to the maximum extent possible; and (b) describe the
   limitations and the code they affect. Such description must be placed in a
   text file included with all distributions of the Covered Software under
   this License. Except to the extent prohibited by statute or regulation,
   such description must be sufficiently detailed for a recipient of ordinary
   skill to be able to understand it.

5. Termination

5.1. The rights granted under this License will terminate automatically if You
     fail to comply with any of its terms. However, if You become compliant,
     then the rights granted under this License from a particular Contributor
     are reinstated (a) provisionally, unless and until such Contributor
     explicitly and finally terminates Your grants, and (b) on an ongoing
     basis, if such Contributor fails to notify You of the non-compliance by
     some reasonable means prior to 60 days after You have come back into
     compliance. Moreover, Your grants from a particular Contributor are
     reinstated on an ongoing basis if such Contributor notifies You of the
     non-compliance by some reasonable means, this is the first time You have
     received notice of non-compliance with this License from such
     Contributor, and You become compliant prior to 30 days after Your receipt
     of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
     infringement claim (excluding declaratory judgment actions,
     counter-claims, and cross-claims) alleging that a Contributor Version
     directly or indirectly infringes any patent, then the rights granted to
     You by any and all Contributors for the Covered Software under Section
     2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user
     license agreements (excluding distributors and resellers) which have been
     validly granted by You or Your distributors under this License prior to
     termination shall survive termination.

6. Disclaimer of Warranty

   Covered Software is provided under this License on an "as is" basis,
   without warranty of any kind, either expressed, implied, or statutory,
   including, without limitation, warranties that the Covered Software is free
   of defects, merchantable, fit for a particular purpose or non-infringing.
   The entire risk as to the quality and performance of the Covered Software
   is with You. Should any Covered Software prove defective in any respect,
   You (not any Contributor) assume the cost of any necessary servicing,
   repair, or correction. This disclaimer of warranty constitutes an essential
   part of this License. No use of  any Covered Software is authorized under
   this License except under this disclaimer.

7. Limitation of Liability

   Under no circumstances and under no legal theory, whether tort (including
   negligence), contract, or otherwise, shall any Contributor, or anyone who
   distributes Covered Software as permitted above, be liable to You for any
   direct, indirect, special, incidental, or consequential damages of any
   character including, without limitation, damages for lost profits, loss of
   goodwill, work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses, even if such party shall have been
   informed of the possibility of such damages. This limitation of liability
   shall not apply to liability for death or personal injury resulting from
   such party's negligence to the extent applicable law prohibits such
   limitation. Some jurisdictions do not allow the exclusion or limitation of
   incidental or consequential damages, so this exclusion and limitation may
   not apply to You.

8. Litigation

   Any litigation relating to this License may be brought only in the courts
   of a jurisdiction where the defendant maintains its principal place of
   business and such litigation shall be governed by laws of that
   jurisdiction, without reference to its conflict-of-law provisions. Nothing
   in this Section shall prevent a party's ability to bring cross-claims or
   counter-claims.

9. Miscellaneous

   This License represents the complete agreement concerning the subject
   matter hereof. If any provision of this License is held to be
   unenforceable, such provision shall be reformed only to the extent
   necessary to make it enforceable. Any law or regulation which provides that
   the language of a contract shall be construed against the drafter shall not
   be used to construe this License against a Contributor.


10. Versions of the License

10.1. New Versions

      Mozilla Foundation is the license steward. Except as provided in Section
      10.3, no one other than the license steward has the right to modify or
      publish new versions of this License. Each version will be given a
      distinguishing version number.

10.2. Effect of New Versions

      You may distribute the Covered Software under the terms of the version
      of the License under which You originally received the Covered Software,
      or under the terms of any subsequent version published by the license
      steward.

10.3. Modified Versions

      If you create software not governed by this License, and you want to
      create a new license for such software, you may create and use a
      modified version of this License if you rename the license and remove
      any references to the name of the license steward (except to note that
      such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
      Licenses If You choose to distribute Source Code Form that is
      Incompatible With Secondary Licenses under the terms of this version of
      the License, the notice described in Exhibit B of this License must be
      attached.

Exhibit A - Source Code Form License Notice

      This Source Code Form is subject to the
      terms of the Mozilla Public License, v.
      2.0. If a copy of the MPL was not
      distributed with this file, You can
      obtain one at
      http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file,
then You may include the notice in a location (such as a LICENSE file in a
relevant directory) where a recipient would be likely to look for such a
notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

      This Source Code Form is "Incompatible
      With Secondary Licenses", as defined by
      the Mozilla Public License, v. 2.0.


```

## legal/license-tool.toml

```text
[overrides]
"backon" = { origin = "https://github.com/Xuanwo/backon" }
"bollard-stubs" = { origin = "https://github.com/fussybeaver/bollard" }
"crunchy" = { origin = "https://github.com/eira-fransham/crunchy" }
"openssl-macros" = { origin = "https://github.com/sfackler/rust-openssl" }
"serde_nanos" = { origin = "https://github.com/caspervonb/serde_nanos" }
"mongodb-internal-macros" = { origin = "https://github.com/mongodb/mongo-rust-driver" }

# rust-license-tool can't find the license for jsonpath-rust 0.5.1
"jsonpath-rust" = { license = "MIT", origin = "https://github.com/besok/jsonpath-rust" }

# `ring` has a custom license that is mostly "ISC-style" but parts of it also fall under OpenSSL licensing.
"ring-0.16.20" = { license = "ISC AND Custom" }
"ring-0.17.5" = { license = "ISC AND Custom" }
"ring-0.17.8" = { license = "ISC AND Custom" }

# `rustls-webpki` doesn't specify their license in the metadata, but the file contains the ISC terms.
"rustls-webpki-0.100.1" = { license = "ISC" }

# `webpki` doesn't specify their license in the metadata, but the file contains the ISC terms.
"webpki-0.22.4" = { license = "ISC" }

# `zerocopy` et al don't specify their licenses in the metadata, but the file contains the 2-clause BSD terms.
"zerocopy-0.6.1" = { license = "BSD-2-Clause" }
"zerocopy-derive-0.3.2" = { license = "BSD-2-Clause" }

```

## legal/licenses/0BSD

```text
BSD Zero Clause License

Copyright (C) YEAR by AUTHOR EMAIL

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

```

## legal/licenses/Apache-2.0

```text
Apache License
Version 2.0, January 2004
http://www.apache.org/licenses/

TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

1. Definitions.
"License" shall mean the terms and conditions for use, reproduction, and distribution as defined by Sections 1 through 9 of this document.

"Licensor" shall mean the copyright owner or entity authorized by the copyright owner that is granting the License.

"Legal Entity" shall mean the union of the acting entity and all other entities that control, are controlled by, or are under common control with that entity. For the purposes of this definition, "control" means (i) the power, direct or indirect, to cause the direction or management of such entity, whether by contract or otherwise, or (ii) ownership of fifty percent (50%) or more of the outstanding shares, or (iii) beneficial ownership of such entity.

"You" (or "Your") shall mean an individual or Legal Entity exercising permissions granted by this License.

"Source" form shall mean the preferred form for making modifications, including but not limited to software source code, documentation source, and configuration files.

"Object" form shall mean any form resulting from mechanical transformation or translation of a Source form, including but not limited to compiled object code, generated documentation, and conversions to other media types.

"Work" shall mean the work of authorship, whether in Source or Object form, made available under the License, as indicated by a copyright notice that is included in or attached to the work (an example is provided in the Appendix below).

"Derivative Works" shall mean any work, whether in Source or Object form, that is based on (or derived from) the Work and for which the editorial revisions, annotations, elaborations, or other modifications represent, as a whole, an original work of authorship. For the purposes of this License, Derivative Works shall not include works that remain separable from, or merely link (or bind by name) to the interfaces of, the Work and Derivative Works thereof.

"Contribution" shall mean any work of authorship, including the original version of the Work and any modifications or additions to that Work or Derivative Works thereof, that is intentionally submitted to Licensor for inclusion in the Work by the copyright owner or by an individual or Legal Entity authorized to submit on behalf of the copyright owner. For the purposes of this definition, "submitted" means any form of electronic, verbal, or written communication sent to the Licensor or its representatives, including but not limited to communication on electronic mailing lists, source code control systems, and issue tracking systems that are managed by, or on behalf of, the Licensor for the purpose of discussing and improving the Work, but excluding communication that is conspicuously marked or otherwise designated in writing by the copyright owner as "Not a Contribution."

"Contributor" shall mean Licensor and any individual or Legal Entity on behalf of whom a Contribution has been received by Licensor and subsequently incorporated within the Work.

2. Grant of Copyright License. Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute the Work and such Derivative Works in Source or Object form.
3. Grant of Patent License. Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable (except as stated in this section) patent license to make, have made, use, offer to sell, sell, import, and otherwise transfer the Work, where such license applies only to those patent claims licensable by such Contributor that are necessarily infringed by their Contribution(s) alone or by combination of their Contribution(s) with the Work to which such Contribution(s) was submitted. If You institute patent litigation against any entity (including a cross-claim or counterclaim in a lawsuit) alleging that the Work or a Contribution incorporated within the Work constitutes direct or contributory patent infringement, then any patent licenses granted to You under this License for that Work shall terminate as of the date such litigation is filed.
4. Redistribution. You may reproduce and distribute copies of the Work or Derivative Works thereof in any medium, with or without modifications, and in Source or Object form, provided that You meet the following conditions:
(a) You must give any other recipients of the Work or Derivative Works a copy of this License; and
(b) You must cause any modified files to carry prominent notices stating that You changed the files; and
(c) You must retain, in the Source form of any Derivative Works that You distribute, all copyright, patent, trademark, and attribution notices from the Source form of the Work, excluding those notices that do not pertain to any part of the Derivative Works; and
(d) If the Work includes a "NOTICE" text file as part of its distribution, then any Derivative Works that You distribute must include a readable copy of the attribution notices contained within such NOTICE file, excluding those notices that do not pertain to any part of the Derivative Works, in at least one of the following places: within a NOTICE text file distributed as part of the Derivative Works; within the Source form or documentation, if provided along with the Derivative Works; or, within a display generated by the Derivative Works, if and wherever such third-party notices normally appear. The contents of the NOTICE file are for informational purposes only and do not modify the License. You may add Your own attribution notices within Derivative Works that You distribute, alongside or as an addendum to the NOTICE text from the Work, provided that such additional attribution notices cannot be construed as modifying the License.
You may add Your own copyright statement to Your modifications and may provide additional or different license terms and conditions for use, reproduction, or distribution of Your modifications, or for any such Derivative Works as a whole, provided Your use, reproduction, and distribution of the Work otherwise complies with the conditions stated in this License.

5. Submission of Contributions. Unless You explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by You to the Licensor shall be under the terms and conditions of this License, without any additional terms or conditions. Notwithstanding the above, nothing herein shall supersede or modify the terms of any separate license agreement you may have executed with Licensor regarding such Contributions.
6. Trademarks. This License does not grant permission to use the trade names, trademarks, service marks, or product names of the Licensor, except as required for reasonable and customary use in describing the origin of the Work and reproducing the content of the NOTICE file.
7. Disclaimer of Warranty. Unless required by applicable law or agreed to in writing, Licensor provides the Work (and each Contributor provides its Contributions) on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied, including, without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE. You are solely responsible for determining the appropriateness of using or redistributing the Work and assume any risks associated with Your exercise of permissions under this License.
8. Limitation of Liability. In no event and under no legal theory, whether in tort (including negligence), contract, or otherwise, unless required by applicable law (such as deliberate and grossly negligent acts) or agreed to in writing, shall any Contributor be liable to You for damages, including any direct, indirect, special, incidental, or consequential damages of any character arising as a result of this License or out of the use or inability to use the Work (including but not limited to damages for loss of goodwill, work stoppage, computer failure or malfunction, or any and all other commercial damages or losses), even if such Contributor has been advised of the possibility of such damages.
9. Accepting Warranty or Additional Liability. While redistributing the Work or Derivative Works thereof, You may choose to offer, and charge a fee for, acceptance of support, warranty, indemnity, or other liability obligations and/or rights consistent with this License. However, in accepting such obligations, You may act only on Your own behalf and on Your sole responsibility, not on behalf of any other Contributor, and only if You agree to indemnify, defend, and hold each Contributor harmless for any liability incurred by, or claims asserted against, such Contributor by reason of your accepting any such warranty or additional liability.
END OF TERMS AND CONDITIONS

APPENDIX: How to apply the Apache License to your work.

To apply the Apache License to your work, attach the following boilerplate notice, with the fields enclosed by brackets "[]" replaced with your own identifying information. (Don't include the brackets!) The text should be enclosed in the appropriate comment syntax for the file format. We also recommend that a file or class name and description of purpose be included on the same "printed page" as the copyright notice for easier identification within third-party archives.

Copyright [yyyy] [name of copyright owner]

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

```

## legal/licenses/BSD-2-Clause

```text
Copyright (c) <year> <owner>.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

```

## legal/licenses/BSD-3-Clause

```text
Copyright (c) <year> <owner>.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

```

## legal/licenses/BSL-1.0

```text
Boost Software License - Version 1.0 - August 17th, 2003

Permission is hereby granted, free of charge, to any person or organization obtaining a copy of the software and accompanying documentation covered by this license (the "Software") to use, reproduce, display, distribute, execute, and transmit the Software, and to prepare derivative works of the Software, and to permit third-parties to whom the Software is furnished to do so, all subject to the following:

The copyright notices in the Software and this entire statement, including the above license grant, this restriction and the following disclaimer, must be included in all copies of the Software, in whole or in part, and all derivative works of the Software, unless such copies or derivative works are solely in the form of machine-executable object code generated by a source language processor.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE AND NON-INFRINGEMENT. IN NO EVENT SHALL THE COPYRIGHT HOLDERS OR ANYONE DISTRIBUTING THE SOFTWARE BE LIABLE FOR ANY DAMAGES OR OTHER LIABILITY, WHETHER IN CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

```

## legal/licenses/BlueOak-1.0.0

```text
# Blue Oak Model License

Version 1.0.0

## Purpose

This license gives everyone as much permission to work with
this software as possible, while protecting contributors
from liability.

## Acceptance

In order to receive this license, you must agree to its
rules.  The rules of this license are both obligations
under that agreement and conditions to your license.
You must not do anything with this software that triggers
a rule that you cannot or will not follow.

## Copyright

Each contributor licenses you to do everything with this
software that would otherwise infringe that contributor's
copyright in it.

## Notices

You must ensure that everyone who gets a copy of
any part of this software from you, with or without
changes, also gets the text of this license or a link to
<https://blueoakcouncil.org/license/1.0.0>.

## Excuse

If anyone notifies you in writing that you have not
complied with [Notices](#notices), you can keep your
license by taking all practical steps to comply within 30
days after the notice.  If you do not do so, your license
ends immediately.

## Patent

Each contributor licenses you to do everything with this
software that would otherwise infringe any patent claims
they can license or become able to license.

## Reliability

No contributor can revoke this license.

## No Liability

***As far as the law allows, this software comes as is,
without any warranty or condition, and no contributor
will be liable to anyone for any damages related to this
software or this license, under any kind of legal claim.***

```

## legal/licenses/CC0-1.0

```text
Creative Commons Legal Code

CC0 1.0 Universal

CREATIVE COMMONS CORPORATION IS NOT A LAW FIRM AND DOES NOT PROVIDE LEGAL SERVICES. DISTRIBUTION OF THIS DOCUMENT DOES NOT CREATE AN ATTORNEY-CLIENT RELATIONSHIP. CREATIVE COMMONS PROVIDES THIS INFORMATION ON AN "AS-IS" BASIS. CREATIVE COMMONS MAKES NO WARRANTIES REGARDING THE USE OF THIS DOCUMENT OR THE INFORMATION OR WORKS PROVIDED HEREUNDER, AND DISCLAIMS LIABILITY FOR DAMAGES RESULTING FROM THE USE OF THIS DOCUMENT OR THE INFORMATION OR WORKS PROVIDED HEREUNDER.

Statement of Purpose

The laws of most jurisdictions throughout the world automatically confer exclusive Copyright and Related Rights (defined below) upon the creator and subsequent owner(s) (each and all, an "owner") of an original work of authorship and/or a database (each, a "Work").

Certain owners wish to permanently relinquish those rights to a Work for the purpose of contributing to a commons of creative, cultural and scientific works ("Commons") that the public can reliably and without fear of later claims of infringement build upon, modify, incorporate in other works, reuse and redistribute as freely as possible in any form whatsoever and for any purposes, including without limitation commercial purposes. These owners may contribute to the Commons to promote the ideal of a free culture and the further production of creative, cultural and scientific works, or to gain reputation or greater distribution for their Work in part through the use and efforts of others.

For these and/or other purposes and motivations, and without any expectation of additional consideration or compensation, the person associating CC0 with a Work (the "Affirmer"), to the extent that he or she is an owner of Copyright and Related Rights in the Work, voluntarily elects to apply CC0 to the Work and publicly distribute the Work under its terms, with knowledge of his or her Copyright and Related Rights in the Work and the meaning and intended legal effect of CC0 on those rights.

1. Copyright and Related Rights. A Work made available under CC0 may be protected by copyright and related or neighboring rights ("Copyright and Related Rights"). Copyright and Related Rights include, but are not limited to, the following:
i. the right to reproduce, adapt, distribute, perform, display, communicate, and translate a Work;
ii. moral rights retained by the original author(s) and/or performer(s);
iii. publicity and privacy rights pertaining to a person's image or likeness depicted in a Work;
iv. rights protecting against unfair competition in regards to a Work, subject to the limitations in paragraph 4(a), below;
v. rights protecting the extraction, dissemination, use and reuse of data in a Work;
vi. database rights (such as those arising under Directive 96/9/EC of the European Parliament and of the Council of 11 March 1996 on the legal protection of databases, and under any national implementation thereof, including any amended or successor version of such directive); and
vii. other similar, equivalent or corresponding rights throughout the world based on applicable law or treaty, and any national implementations thereof.
2. Waiver. To the greatest extent permitted by, but not in contravention of, applicable law, Affirmer hereby overtly, fully, permanently, irrevocably and unconditionally waives, abandons, and surrenders all of Affirmer's Copyright and Related Rights and associated claims and causes of action, whether now known or unknown (including existing as well as future claims and causes of action), in the Work (i) in all territories worldwide, (ii) for the maximum duration provided by applicable law or treaty (including future time extensions), (iii) in any current or future medium and for any number of copies, and (iv) for any purpose whatsoever, including without limitation commercial, advertising or promotional purposes (the "Waiver"). Affirmer makes the Waiver for the benefit of each member of the public at large and to the detriment of Affirmer's heirs and successors, fully intending that such Waiver shall not be subject to revocation, rescission, cancellation, termination, or any other legal or equitable action to disrupt the quiet enjoyment of the Work by the public as contemplated by Affirmer's express Statement of Purpose.
3. Public License Fallback. Should any part of the Waiver for any reason be judged legally invalid or ineffective under applicable law, then the Waiver shall be preserved to the maximum extent permitted taking into account Affirmer's express Statement of Purpose. In addition, to the extent the Waiver is so judged Affirmer hereby grants to each affected person a royalty-free, non transferable, non sublicensable, non exclusive, irrevocable and unconditional license to exercise Affirmer's Copyright and Related Rights in the Work (i) in all territories worldwide, (ii) for the maximum duration provided by applicable law or treaty (including future time extensions), (iii) in any current or future medium and for any number of copies, and (iv) for any purpose whatsoever, including without limitation commercial, advertising or promotional purposes (the "License"). The License shall be deemed effective as of the date CC0 was applied by Affirmer to the Work. Should any part of the License for any reason be judged legally invalid or ineffective under applicable law, such partial invalidity or ineffectiveness shall not invalidate the remainder of the License, and in such case Affirmer hereby affirms that he or she will not (i) exercise any of his or her remaining Copyright and Related Rights in the Work or (ii) assert any associated claims and causes of action with respect to the Work, in either case contrary to Affirmer's express Statement of Purpose.
4. Limitations and Disclaimers.
a. No trademark or patent rights held by Affirmer are waived, abandoned, surrendered, licensed or otherwise affected by this document.
b. Affirmer offers the Work as-is and makes no representations or warranties of any kind concerning the Work, express, implied, statutory or otherwise, including without limitation warranties of title, merchantability, fitness for a particular purpose, non infringement, or the absence of latent or other defects, accuracy, or the present or absence of errors, whether or not discoverable, all to the greatest extent permissible under applicable law.
c. Affirmer disclaims responsibility for clearing rights of other persons that may apply to the Work or any use thereof, including without limitation any person's Copyright and Related Rights in the Work. Further, Affirmer disclaims responsibility for obtaining any necessary consents, permissions or other rights required for any use of the Work.
d. Affirmer understands and acknowledges that Creative Commons is not a party to this document and has no duty or obligation with respect to this CC0 or use of the Work.

```

## legal/licenses/CDLA-Permissive-2.0

```text
Community Data License Agreement - Permissive - Version 2.0

This is the Community Data License Agreement - Permissive, Version 2.0 (the "agreement"). Data Provider(s) and Data Recipient(s) agree as follows:

1. Provision of the Data

1.1. A Data Recipient may use, modify, and share the Data made available by Data Provider(s) under this agreement if that Data Recipient follows the terms of this agreement.

1.2. This agreement does not impose any restriction on a Data Recipient's use, modification, or sharing of any portions of the Data that are in the public domain or that may be used, modified, or shared under any other legal exception or limitation.

2. Conditions for Sharing Data

2.1. A Data Recipient may share Data, with or without modifications, so long as the Data Recipient makes available the text of this agreement with the shared Data.

3. No Restrictions on Results

3.1. This agreement does not impose any restriction or obligations with respect to the use, modification, or sharing of Results.

4. No Warranty; Limitation of Liability

4.1. All Data Recipients receive the Data subject to the following terms:

THE DATA IS PROVIDED ON AN "AS IS" BASIS, WITHOUT REPRESENTATIONS, WARRANTIES OR CONDITIONS OF ANY KIND, EITHER EXPRESS OR IMPLIED INCLUDING, WITHOUT LIMITATION, ANY WARRANTIES OR CONDITIONS OF TITLE, NON-INFRINGEMENT, MERCHANTABILITY OR FITNESS FOR A PARTICULAR PURPOSE.

NO DATA PROVIDER SHALL HAVE ANY LIABILITY FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING WITHOUT LIMITATION LOST PROFITS), HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE DATA OR RESULTS, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

5. Definitions

5.1. "Data" means the material received by a Data Recipient under this agreement.

5.2. "Data Provider" means any person who is the source of Data provided under this agreement and in reliance on a Data Recipient's agreement to its terms.

5.3. "Data Recipient" means any person who receives Data directly or indirectly from a Data Provider and agrees to the terms of this agreement.

5.4. "Results" means any outcome obtained by computational analysis of Data, including for example machine learning models and models' insights.

```

## legal/licenses/ISC

```text
ISC License

<copyright notice>

Permission to use, copy, modify, and /or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

```

## legal/licenses/MIT

```text
MIT license

Copyright (c) <year> <copyright holders>

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice (including the next paragraph) shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

```

## legal/licenses/MIT-0

```text
MIT No Attribution

Copyright <YEAR> <COPYRIGHT HOLDER>

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

```

## legal/licenses/MPL-2.0

```text
Mozilla Public License Version 2.0

==================================

1. Definitions

--------------

1.1. "Contributor" means each individual or legal entity that creates, contributes to the creation of, or owns Covered Software.
1.2. "Contributor Version" means the combination of the Contributions of others (if any) used by a Contributor and that particular Contributor's Contribution.
1.3. "Contribution" means Covered Software of a particular Contributor.
1.4. "Covered Software" means Source Code Form to which the initial Contributor has attached the notice in Exhibit A, the Executable Form of such Source Code Form, and Modifications of such Source Code Form, in each case including portions thereof.
1.5. "Incompatible With Secondary Licenses" means
(a) that the initial Contributor has attached the notice described in Exhibit B to the Covered Software; or
(b) that the Covered Software was made available under the terms of version 1.1 or earlier of the License, but not also under the terms of a Secondary License.
1.6. "Executable Form" means any form of the work other than Source Code Form.
1.7. "Larger Work" means a work that combines Covered Software with other material, in a separate file or files, that is not Covered Software.
1.8. "License" means this document.
1.9. "Licensable" means having the right to grant, to the maximum extent possible, whether at the time of the initial grant or subsequently, any and all of the rights conveyed by this License.
1.10. "Modifications" means any of the following:
(a) any file in Source Code Form that results from an addition to, deletion from, or modification of the contents of Covered Software; or
(b) any new file in Source Code Form that contains any Covered Software.
1.11. "Patent Claims" of a Contributor means any patent claim(s), including without limitation, method, process, and apparatus claims, in any patent Licensable by such Contributor that would be infringed, but for the grant of the License, by the making, using, selling, offering for sale, having made, import, or transfer of either its Contributions or its Contributor Version.
1.12. "Secondary License" means either the GNU General Public License, Version 2.0, the GNU Lesser General Public License, Version 2.1, the GNU Affero General Public License, Version 3.0, or any later versions of those licenses.
1.13. "Source Code Form" means the form of the work preferred for making modifications.
1.14. "You" (or "Your") means an individual or a legal entity exercising rights under this License. For legal entities, "You" includes any entity that controls, is controlled by, or is under common control with You. For purposes of this definition, "control" means (a) the power, direct or indirect, to cause the direction or management of such entity, whether by contract or otherwise, or (b) ownership of more than fifty percent (50%) of the outstanding shares or beneficial ownership of such entity.
2. License Grants and Conditions

--------------------------------

2.1. Grants
Each Contributor hereby grants You a world-wide, royalty-free, non-exclusive license:

(a) under intellectual property rights (other than patent or trademark) Licensable by such Contributor to use, reproduce, make available, modify, display, perform, distribute, and otherwise exploit its Contributions, either on an unmodified basis, with Modifications, or as part of a Larger Work; and
(b) under Patent Claims of such Contributor to make, use, sell, offer for sale, have made, import, and otherwise transfer either its Contributions or its Contributor Version.
2.2. Effective Date
The licenses granted in Section 2.1 with respect to any Contribution become effective for each Contribution on the date the Contributor first distributes such Contribution.

2.3. Limitations on Grant Scope
The licenses granted in this Section 2 are the only rights granted under this License. No additional rights or licenses will be implied from the distribution or licensing of Covered Software under this License. Notwithstanding Section 2.1(b) above, no patent license is granted by a Contributor:

(a) for any code that a Contributor has removed from Covered Software; or
(b) for infringements caused by: (i) Your and any other third party's modifications of Covered Software, or (ii) the combination of its Contributions with other software (except as part of its Contributor Version); or
(c) under Patent Claims infringed by Covered Software in the absence of its Contributions.
This License does not grant any rights in the trademarks, service marks, or logos of any Contributor (except as may be necessary to comply with the notice requirements in Section 3.4).

2.4. Subsequent Licenses
No Contributor makes additional grants as a result of Your choice to distribute the Covered Software under a subsequent version of this License (see Section 10.2) or under the terms of a Secondary License (if permitted under the terms of Section 3.3).

2.5. Representation
Each Contributor represents that the Contributor believes its Contributions are its original creation(s) or it has sufficient rights to grant the rights to its Contributions conveyed by this License.

2.6. Fair Use
This License is not intended to limit any rights You have under applicable copyright doctrines of fair use, fair dealing, or other equivalents.

2.7. Conditions
Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted in Section 2.1.

3. Responsibilities

-------------------

3.1. Distribution of Source Form
All distribution of Covered Software in Source Code Form, including any Modifications that You create or to which You contribute, must be under the terms of this License. You must inform recipients that the Source Code Form of the Covered Software is governed by the terms of this License, and how they can obtain a copy of this License. You may not attempt to alter or restrict the recipients' rights in the Source Code Form.

3.2. Distribution of Executable Form
If You distribute Covered Software in Executable Form then:

(a) such Covered Software must also be made available in Source Code Form, as described in Section 3.1, and You must inform recipients of the Executable Form how they can obtain a copy of such Source Code Form by reasonable means in a timely manner, at a charge no more than the cost of distribution to the recipient; and
(b) You may distribute such Executable Form under the terms of this License, or sublicense it under different terms, provided that the license for the Executable Form does not attempt to limit or alter the recipients' rights in the Source Code Form under this License.
3.3. Distribution of a Larger Work
You may create and distribute a Larger Work under terms of Your choice, provided that You also comply with the requirements of this License for the Covered Software. If the Larger Work is a combination of Covered Software with a work governed by one or more Secondary Licenses, and the Covered Software is not Incompatible With Secondary Licenses, this License permits You to additionally distribute such Covered Software under the terms of such Secondary License(s), so that the recipient of the Larger Work may, at their option, further distribute the Covered Software under the terms of either this License or such Secondary License(s).

3.4. Notices
You may not remove or alter the substance of any license notices (including copyright notices, patent notices, disclaimers of warranty, or limitations of liability) contained within the Source Code Form of the Covered Software, except that You may alter any license notices to the extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms
You may choose to offer, and to charge a fee for, warranty, support, indemnity or liability obligations to one or more recipients of Covered Software. However, You may do so only on Your own behalf, and not on behalf of any Contributor. You must make it absolutely clear that any such warranty, support, indemnity, or liability obligation is offered by You alone, and You hereby agree to indemnify every Contributor for any liability incurred by such Contributor as a result of warranty, support, indemnity or liability terms You offer. You may include additional disclaimers of warranty and limitations of liability specific to any jurisdiction.

4. Inability to Comply Due to Statute or Regulation

---------------------------------------------------

If it is impossible for You to comply with any of the terms of this License with respect to some or all of the Covered Software due to statute, judicial order, or regulation then You must: (a) comply with the terms of this License to the maximum extent possible; and (b) describe the limitations and the code they affect. Such description must be placed in a text file included with all distributions of the Covered Software under this License. Except to the extent prohibited by statute or regulation, such description must be sufficiently detailed for a recipient of ordinary skill to be able to understand it.

5. Termination

--------------

5.1. The rights granted under this License will terminate automatically if You fail to comply with any of its terms. However, if You become compliant, then the rights granted under this License from a particular Contributor are reinstated (a) provisionally, unless and until such Contributor explicitly and finally terminates Your grants, and (b) on an ongoing basis, if such Contributor fails to notify You of the non-compliance by some reasonable means prior to 60 days after You have come back into compliance. Moreover, Your grants from a particular Contributor are reinstated on an ongoing basis if such Contributor notifies You of the non-compliance by some reasonable means, this is the first time You have received notice of non-compliance with this License from such Contributor, and You become compliant prior to 30 days after Your receipt of the notice.
5.2. If You initiate litigation against any entity by asserting a patent infringement claim (excluding declaratory judgment actions, counter-claims, and cross-claims) alleging that a Contributor Version directly or indirectly infringes any patent, then the rights granted to You by any and all Contributors for the Covered Software under Section 2.1 of this License shall terminate.
5.3. In the event of termination under Sections 5.1 or 5.2 above, all end user license agreements (excluding distributors and resellers) which have been validly granted by You or Your distributors under this License prior to termination shall survive termination.
************************************************************************

6. Disclaimer of Warranty

* ------------------------- *

Covered Software is provided under this License on an "as is" basis, without warranty of any kind, either expressed, implied, or statutory, including, without limitation, warranties that the Covered Software is free of defects, merchantable, fit for a particular purpose or non-infringing. The entire risk as to the quality and performance of the Covered Software is with You. Should any Covered Software prove defective in any respect, You (not any Contributor) assume the cost of any necessary servicing, repair, or correction. This disclaimer of warranty constitutes an essential part of this License. No use of any Covered Software is authorized under this License except under this disclaimer.

************************************************************************

************************************************************************

7. Limitation of Liability

* -------------------------- *

Under no circumstances and under no legal theory, whether tort (including negligence), contract, or otherwise, shall any Contributor, or anyone who distributes Covered Software as permitted above, be liable to You for any direct, indirect, special, incidental, or consequential damages of any character including, without limitation, damages for lost profits, loss of goodwill, work stoppage, computer failure or malfunction, or any and all other commercial damages or losses, even if such party shall have been informed of the possibility of such damages. This limitation of liability shall not apply to liability for death or personal injury resulting from such party's negligence to the extent applicable law prohibits such limitation. Some jurisdictions do not allow the exclusion or limitation of incidental or consequential damages, so this exclusion and limitation may not apply to You.

************************************************************************

8. Litigation

-------------

Any litigation relating to this License may be brought only in the courts of a jurisdiction where the defendant maintains its principal place of business and such litigation shall be governed by laws of that jurisdiction, without reference to its conflict-of-law provisions. Nothing in this Section shall prevent a party's ability to bring cross-claims or counter-claims.

9. Miscellaneous

----------------

This License represents the complete agreement concerning the subject matter hereof. If any provision of this License is held to be unenforceable, such provision shall be reformed only to the extent necessary to make it enforceable. Any law or regulation which provides that the language of a contract shall be construed against the drafter shall not be used to construe this License against a Contributor.

10. Versions of the License

---------------------------

10.1. New Versions
Mozilla Foundation is the license steward. Except as provided in Section 10.3, no one other than the license steward has the right to modify or publish new versions of this License. Each version will be given a distinguishing version number.

10.2. Effect of New Versions
You may distribute the Covered Software under the terms of the version of the License under which You originally received the Covered Software, or under the terms of any subsequent version published by the license steward.

10.3. Modified Versions
If you create software not governed by this License, and you want to create a new license for such software, you may create and use a modified version of this License if you rename the license and remove any references to the name of the license steward (except to note that such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary Licenses
If You choose to distribute Source Code Form that is Incompatible With Secondary Licenses under the terms of this version of the License, the notice described in Exhibit B of this License must be attached.

Exhibit A - Source Code Form License Notice

-------------------------------------------

This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular file, then You may include the notice in a location (such as a LICENSE file in a relevant directory) where a recipient would be likely to look for such a notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice

---------------------------------------------------------

This Source Code Form is "Incompatible With Secondary Licenses", as defined by the Mozilla Public License, v. 2.0.

```

## legal/licenses/OpenSSL

```text
OpenSSL License

Copyright (c) 1998-2008 The OpenSSL Project. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. All advertising materials mentioning features or use of this software must display the following acknowledgment: "This product includes software developed by the OpenSSL Project for use in the OpenSSL Toolkit. (http://www.openssl.org/)"
4. The names "OpenSSL Toolkit" and "OpenSSL Project" must not be used to endorse or promote products derived from this software without prior written permission. For written permission, please contact openssl-core@openssl.org.
5. Products derived from this software may not be called "OpenSSL" nor may "OpenSSL" appear in their names without prior written permission of the OpenSSL Project.
6. Redistributions of any form whatsoever must retain the following acknowledgment: "This product includes software developed by the OpenSSL Project for use in the OpenSSL Toolkit (http://www.openssl.org/)"
THIS SOFTWARE IS PROVIDED BY THE OpenSSL PROJECT ``AS IS'' AND ANY EXPRESSED OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE OpenSSL PROJECT OR ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

This product includes cryptographic software written by Eric Young (eay@cryptsoft.com). This product includes software written by Tim Hudson (tjh@cryptsoft.com).

Original SSLeay License

Copyright (C) 1995-1998 Eric Young (eay@cryptsoft.com)
All rights reserved.

This package is an SSL implementation written by Eric Young (eay@cryptsoft.com). The implementation was written so as to conform with Netscapes SSL.

This library is free for commercial and non-commercial use as long as the following conditions are aheared to. The following conditions apply to all code found in this distribution, be it the RC4, RSA, lhash, DES, etc., code; not just the SSL code. The SSL documentation included with this distribution is covered by the same copyright terms except that the holder is Tim Hudson (tjh@cryptsoft.com).

Copyright remains Eric Young's, and as such any Copyright notices in the code are not to be removed. If this package is used in a product, Eric Young should be given attribution as the author of the parts of the library used. This can be in the form of a textual message at program startup or in documentation (online or textual) provided with the package.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. All advertising materials mentioning features or use of this software must display the following acknowledgement:
"This product includes cryptographic software written by Eric Young (eay@cryptsoft.com)"
The word 'cryptographic' can be left out if the rouines from the library being used are not cryptographic related :-).
4. If you include any Windows specific code (or a derivative thereof) from the apps directory (application code) you must include an acknowledgement: "This product includes software written by Tim Hudson (tjh@cryptsoft.com)"
THIS SOFTWARE IS PROVIDED BY ERIC YOUNG ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The licence and distribution terms for any publically available version or derivative of this code cannot be changed. i.e. this code cannot simply be copied and put under another distribution licence [including the GNU Public Licence.]

```

## legal/licenses/Unicode-3.0

```text
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2026 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.

```

## legal/licenses/Unicode-DFS-2016

```text
UNICODE, INC. LICENSE AGREEMENT - DATA FILES AND SOFTWARE

See Terms of Use for definitions of Unicode Inc.'s Data Files and Software.
Unicode Data Files include all data files under the directories http://www.unicode.org/Public/, http://www.unicode.org/reports/, http://www.unicode.org/cldr/data/, http://source.icu-project.org/repos/icu/, http://www.unicode.org/ivd/data/, and http://www.unicode.org/utility/trac/browser/.

Unicode Data Files do not include PDF online code charts under the directory http://www.unicode.org/Public/.

Software includes any source code published in the Unicode Standard or under the directories http://www.unicode.org/Public/, http://www.unicode.org/reports/, http://www.unicode.org/cldr/data/, http://source.icu-project.org/repos/icu/, and http://www.unicode.org/utility/trac/browser/.

NOTICE TO USER: Carefully read the following legal agreement. BY DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING UNICODE INC.'S DATA FILES ("DATA FILES"), AND/OR SOFTWARE ("SOFTWARE"), YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2016 Unicode, Inc. All rights reserved. Distributed under the Terms of Use in http://www.unicode.org/copyright.html.

Permission is hereby granted, free of charge, to any person obtaining a copy of the Unicode data files and any associated documentation (the "Data Files") or Unicode software and any associated documentation (the "Software") to deal in the Data Files or Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, and/or sell copies of the Data Files or Software, and to permit persons to whom the Data Files or Software are furnished to do so, provided that either

(a) this copyright and permission notice appear with all copies of the Data Files or Software, or
(b) this copyright and permission notice appear in associated Documentation.
THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF THIRD PARTY RIGHTS. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES, OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall not be used in advertising or otherwise to promote the sale, use or other dealings in these Data Files or Software without prior written authorization of the copyright holder.

```

## legal/licenses/Zlib

```text
zlib License

Copyright (c) <year> <copyright holders>

This software is provided 'as-is', without any express or implied warranty. In no event will the authors be held liable for any damages arising from the use of this software.

Permission is granted to anyone to use this software for any purpose, including commercial applications, and to alter it and redistribute it freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not claim that you wrote the original software. If you use this software in a product, an acknowledgment in the product documentation would be appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be misrepresented as being the original software.
3. This notice may not be removed or altered from any source distribution.

```

## legal/proto/third-party/google/LICENSE-Apache-2.0.txt

```text

                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

```

## legal/proto/third-party/google/protobuf/LICENSE

```text
Copyright 2008 Google Inc.  All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

    * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
    * Neither the name of Google Inc. nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Code generated by the Protocol Buffer compiler is owned by the owner
of the input file used when generating it.  This code is not
standalone and requires a support library to be linked with it.  This
support library is itself covered by the above license.

```

## legal/vdev/src/commands/build/licenses.rs

```text
use anyhow::Result;

use crate::app;

/// Rebuild the 3rd-party license file.
#[derive(clap::Args, Debug)]
#[command()]
pub struct Cli {}

impl Cli {
    pub fn exec(self) -> Result<()> {
        app::exec("dd-rust-license-tool", ["write"], true)
    }
}

```

## legal/vdev/src/commands/check/licenses.rs

```text
use anyhow::Result;

use crate::app;

/// Check that the 3rd-party license file is up to date
#[derive(clap::Args, Debug)]
#[command()]
pub struct Cli {}

impl Cli {
    pub fn exec(self) -> Result<()> {
        app::exec("dd-rust-license-tool", ["check"], true).inspect_err(|_| {
            info!("Run `cargo vdev build licenses` to regenerate the file");
        })
    }
}

```
