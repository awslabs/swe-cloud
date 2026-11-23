# Attribution

Upstream: https://github.com/redpanda-data/connect
Revision: 9e107a9aa94bcfaa5325fc51e0ed050b80eeed03

Licenses and notices for distribution only; not installed in task containers.

## legal/internal/license/service.go

```text
// Copyright 2024 Redpanda Data, Inc.
//
// Licensed as a Redpanda Enterprise file under the Redpanda Community
// License (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
//
// https://github.com/redpanda-data/connect/blob/main/licenses/rcl.md

package license

import (
	"context"
	_ "embed"
	"fmt"
	"os"
	"sync/atomic"
	"time"

	"github.com/redpanda-data/benthos/v4/public/service"
	"github.com/redpanda-data/common-go/license"
)

const defaultLicenseFilepath = "/etc/redpanda/redpanda.license"

var openSourceLicense license.RedpandaLicense = &license.V1RedpandaLicense{
	Version:  1,
	Type:     license.LicenseTypeOpenSource,
	Expiry:   time.Now().Add(time.Hour * 24 * 365 * 10).Unix(),
	Products: []license.Product{license.ProductConnect},
}

// Service is the license service.
type Service struct {
	logger        *service.Logger
	loadedLicense *atomic.Pointer[license.RedpandaLicense]
	conf          Config

	expiryMetric *service.MetricGauge
	cancel       context.CancelFunc
}

// Config is a struct used to provide configuration to a license service.
type Config struct {
	License                      string
	LicenseFilepath              string
	customDefaultLicenseFilepath string
}

func (c Config) defaultLicenseFilepath() string {
	if c.customDefaultLicenseFilepath != "" {
		return c.customDefaultLicenseFilepath
	}
	return defaultLicenseFilepath
}

// RegisterService creates a new license service and registers it to the
// provided resources pointer.
func RegisterService(res *service.Resources, conf Config) {
	s := &Service{
		logger:        res.Logger(),
		loadedLicense: &atomic.Pointer[license.RedpandaLicense]{},
		conf:          conf,
	}

	license, err := s.readAndValidateLicense()
	if err != nil {
		res.Logger().With("error", err).Error("Failed to read Redpanda License")
		license = openSourceLicense
	}

	s.setLicense(res, license)
	setSharedService(res, s)
}

// InjectTestService inserts an enterprise license into a resources pointer in
// order to provide testing frameworks a way to test enterprise components.
func InjectTestService(res *service.Resources) {
	s := &Service{
		logger:        res.Logger(),
		loadedLicense: &atomic.Pointer[license.RedpandaLicense]{},
	}

	s.setLicense(res, &license.V1RedpandaLicense{
		Version:      1,
		Organization: "test",
		Type:         license.LicenseTypeEnterprise,
		Expiry:       time.Now().Add(time.Hour).Unix(),
		Products:     []license.Product{license.ProductConnect},
	})
	setSharedService(res, s)
}

// InjectCustomLicenseBytes attempts to parse a Redpanda Enterprise license
// from a slice of bytes and, if successful, stores it within the provided
// resources pointer for enterprise components to reference.
func InjectCustomLicenseBytes(res *service.Resources, conf Config, licenseBytes []byte) error {
	s := &Service{
		logger:        res.Logger(),
		loadedLicense: &atomic.Pointer[license.RedpandaLicense]{},
		conf:          conf,
	}

	l, err := license.ParseLicense(licenseBytes)
	if err != nil {
		return fmt.Errorf("validating license: %w", err)
	}

	expiryTime := l.Expires()
	if time.Now().After(expiryTime) {
		return fmt.Errorf("license expired on %s", expiryTime.Format(time.RFC3339))
	}

	var orgStr, licenseTypeStr string
	switch t := l.(type) {
	case *license.V0RedpandaLicense:
		orgStr = t.Organization
		licenseTypeStr = t.Type.String()
	case *license.V1RedpandaLicense:
		orgStr = t.Organization
		licenseTypeStr = string(t.Type)
	}

	s.logger.With(
		"license_org", orgStr,
		"license_type", licenseTypeStr,
		"expires_at", expiryTime.Format(time.RFC3339),
	).Info("Successfully loaded Redpanda license")

	s.setLicense(res, l)
	setSharedService(res, s)

	return nil
}

func (s *Service) setLicense(res *service.Resources, l license.RedpandaLicense) {
	s.loadedLicense.Store(&l)

	if s.cancel != nil {
		s.cancel()
	}
	if l == nil || !l.AllowsEnterpriseFeatures() {
		return
	}

	if s.expiryMetric == nil {
		s.expiryMetric = res.Metrics().NewGauge("redpanda_cluster_features_enterprise_license_expiry_sec")
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	go s.updateExpiryMetricLoop(ctx, l)
}

// updateExpiryMetricLoop updates the license expiry metric every hour. The
// metric value is the delta in seconds between now and the expiry time.
func (s *Service) updateExpiryMetricLoop(ctx context.Context, l license.RedpandaLicense) {
	updateMetric := func() {
		expiryTime := l.Expires()
		deltaSeconds := time.Until(expiryTime).Seconds()
		s.expiryMetric.Set(int64(deltaSeconds))
	}
	updateMetric()

	t := time.NewTicker(time.Hour)
	defer t.Stop()
	for {
		select {
		case <-t.C:
			updateMetric()
		case <-ctx.Done():
			return
		}
	}
}

func (s *Service) readAndValidateLicense() (license.RedpandaLicense, error) {
	licenseBytes, err := s.readLicense()
	if err != nil {
		return nil, err
	}

	l := openSourceLicense
	if len(licenseBytes) > 0 {
		if l, err = license.ParseLicense(licenseBytes); err != nil {
			return nil, fmt.Errorf("validating license: %w", err)
		}
	}

	expiryTime := l.Expires()
	if time.Now().After(expiryTime) {
		return nil, fmt.Errorf("license expired on %s", expiryTime.Format(time.RFC3339))
	}

	var orgStr, licenseTypeStr string
	switch t := l.(type) {
	case *license.V0RedpandaLicense:
		orgStr = t.Organization
		licenseTypeStr = t.Type.String()
	case *license.V1RedpandaLicense:
		orgStr = t.Organization
		licenseTypeStr = string(t.Type)
	}

	s.logger.With(
		"license_org", orgStr,
		"license_type", licenseTypeStr,
		"expires_at", expiryTime.Format(time.RFC3339),
	).Info("Successfully loaded Redpanda license")

	return l, nil
}

func (s *Service) readLicense() (licenseFileContents []byte, err error) {
	// Explicit license takes priority.
	if s.conf.License != "" {
		s.logger.Debug("Loading explicitly defined Redpanda Enterprise license")

		licenseFileContents = []byte(s.conf.License)
		return
	}

	// Followed by explicit license file path.
	if s.conf.LicenseFilepath != "" {
		s.logger.Debug("Loading Redpanda Enterprise license from explicit file path")

		licenseFileContents, err = os.ReadFile(s.conf.LicenseFilepath)
		if err != nil {
			return nil, fmt.Errorf("reading license file: %w", err)
		}
		return
	}

	// Followed by the default file path.
	if licenseFileContents, err = os.ReadFile(s.conf.defaultLicenseFilepath()); err != nil {
		if !os.IsNotExist(err) {
			return nil, fmt.Errorf("reading default path license file: %w", err)
		}
		return nil, nil
	}

	s.logger.Debug("Loaded Redpanda Enterprise license from default file path")
	return
}

```

## legal/internal/license/service_test.go

```text
// Copyright 2024 Redpanda Data, Inc.
//
// Licensed as a Redpanda Enterprise file under the Redpanda Community
// License (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
//
// https://github.com/redpanda-data/connect/blob/main/licenses/rcl.md

package license

import (
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/redpanda-data/benthos/v4/public/service"
	"github.com/redpanda-data/common-go/license"
)

func TestLicenseEnterpriseNoLicense(t *testing.T) {
	tmpDir := t.TempDir()
	tmpBadLicensePath := filepath.Join(tmpDir, "bad.license")

	res := service.MockResources()
	RegisterService(res, Config{
		customDefaultLicenseFilepath: tmpBadLicensePath,
	})

	loaded, err := LoadFromResources(res)
	require.NoError(t, err)

	assert.False(t, loaded.AllowsEnterpriseFeatures())
}

func TestCheckRunningEnterprise(t *testing.T) {
	validExpiry := time.Now().Add(time.Hour).Unix()
	pastExpiry := time.Now().Add(-time.Hour).Unix()

	tests := []struct {
		name string
		// license is the license the license service holds. Nil means that no
		// license service is registered, as in redpanda-connect-community.
		license license.RedpandaLicense
		// wantErr is a substring of the expected error. Empty means no error.
		wantErr string
	}{
		{
			name:    "no license service",
			license: nil,
			wantErr: "this feature requires a valid Redpanda Enterprise Edition license that includes the Connect product",
		},
		{
			name:    "open source license",
			license: openSourceLicense,
			wantErr: "this feature requires a valid Redpanda Enterprise Edition license that includes the Connect product",
		},
		{
			name: "v1 enterprise license with Connect",
			license: &license.V1RedpandaLicense{
				Type:     license.LicenseTypeEnterprise,
				Expiry:   validExpiry,
				Products: []license.Product{license.ProductConnect},
			},
			wantErr: "",
		},
		{
			name: "v1 free trial license with Connect",
			license: &license.V1RedpandaLicense{
				Type:     license.LicenseTypeFreeTrial,
				Expiry:   validExpiry,
				Products: []license.Product{license.ProductConnect},
			},
			wantErr: "",
		},
		{
			name: "v1 enterprise license without Connect",
			license: &license.V1RedpandaLicense{
				Type:     license.LicenseTypeEnterprise,
				Expiry:   validExpiry,
				Products: []license.Product{"OTHER"},
			},
			wantErr: "this feature requires a valid Redpanda Enterprise Edition license that includes the Connect product",
		},
		{
			name: "v1 expired enterprise license with Connect",
			license: &license.V1RedpandaLicense{
				Type:     license.LicenseTypeEnterprise,
				Expiry:   pastExpiry,
				Products: []license.Product{license.ProductConnect},
			},
			wantErr: "this feature requires a valid Redpanda Enterprise Edition license that includes the Connect product",
		},
		{
			name: "v0 enterprise license",
			license: &license.V0RedpandaLicense{
				Type:   license.V0LicenseTypeEnterprise,
				Expiry: validExpiry,
			},
			wantErr: "",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			res := service.MockResources()
			if test.license != nil {
				s := &Service{
					logger:        res.Logger(),
					loadedLicense: &atomic.Pointer[license.RedpandaLicense]{},
				}
				s.setLicense(res, test.license)
				// setLicense starts an expiry metric loop only for licenses that
				// allow enterprise features.
				if s.cancel != nil {
					t.Cleanup(s.cancel)
				}
				setSharedService(res, s)
			}

			err := CheckRunningEnterprise(res)
			if test.wantErr == "" {
				require.NoError(t, err)
			} else {
				require.ErrorContains(t, err, test.wantErr)
			}
		})
	}
}

```

## legal/internal/license/shared_service.go

```text
// Copyright 2024 Redpanda Data, Inc.
//
// Licensed as a Redpanda Enterprise file under the Redpanda Community
// License (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
//
// https://github.com/redpanda-data/connect/blob/main/licenses/rcl.md

package license

import (
	"errors"

	"github.com/redpanda-data/benthos/v4/public/service"
	"github.com/redpanda-data/common-go/license"
)

// LoadFromResources attempts to access a license service from a provided
// resources handle and returns the current license it tracks. An error is
// returned if the license service cannot be accessed or cannot provide license
// information.
func LoadFromResources(res *service.Resources) (license.RedpandaLicense, error) {
	svc := getSharedService(res)
	if svc == nil {
		return nil, errors.New("unable to access license service")
	}

	l := svc.loadedLicense.Load()
	if l == nil {
		return nil, errors.New("unable to access license information")
	}

	return *l, nil
}

// CheckRunningEnterprise returns a non-nil error if the instance of Redpanda
// Connect is not operating with a valid enterprise license.
func CheckRunningEnterprise(res *service.Resources) error {
	// A binary that does not register a license service, such as
	// redpanda-connect-community, cannot run enterprise features. Report this
	// as a missing license and not as an internal fault, because the
	// community bundle can import enterprise components.
	if getSharedService(res) == nil {
		return errEnterpriseLicenseRequired
	}
	l, err := LoadFromResources(res)
	if err != nil {
		return err
	}
	if !l.AllowsEnterpriseFeatures() || !l.IncludesProduct(license.ProductConnect) {
		return errEnterpriseLicenseRequired
	}
	return nil
}

var errEnterpriseLicenseRequired = errors.New("this feature requires a valid Redpanda Enterprise Edition license that includes the Connect product. For more information check out: https://docs.redpanda.com/connect/get-started/licensing/")

type sharedServiceKeyType int

var sharedServiceKey sharedServiceKeyType

func setSharedService(res *service.Resources, svc *Service) {
	res.SetGeneric(sharedServiceKey, svc)
}

func getSharedService(res *service.Resources) *Service {
	reg, _ := res.GetGeneric(sharedServiceKey)
	if reg == nil {
		return nil
	}
	return reg.(*Service)
}

```

## legal/licenses/Apache-2.0.txt

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

## legal/licenses/Apache-2.0_header.go.txt

```text
// Copyright 2024 Redpanda Data, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//    http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.


```

## legal/licenses/README.md

```text
# FAQ

There are 2 licenses for Redpanda Connect. Apache-2.0 covers the majority of connectors and functionality, and RCL (Redpanda Community License)
which covers enterprise features.

1. [Apache-2.0](Apache-2.0.txt): Covers the majority of connectors and functionality.

2. [RCL](rcl.md): Redpanda Community License - is intended to allow you to use enterprise features
that you pay for.

```

## legal/licenses/cla.md

```text
**Redpanda Data, Inc.**

**Redpanda Contributor License Agreement**

Thank you for your interest in the open source project(s) managed by
Redpanda Data, Inc. (“Redpanda Data”). In order to clarify the intellectual
property license granted with Contributions from any person or entity,
Redpanda Data must have a Contributor License Agreement (“CLA”) on file
that has been entered into by each contributor, indicating agreement to
the license terms below. This license is for your protection as a
contributor as well as the protection of Redpanda Data and its other
contributors and users; it does not change your rights to use your own
Contributions for any other purpose.

By clicking “Accept” You accept and agree to these terms and conditions
for Your present and future Contributions submitted to Redpanda Data. In
return, Redpanda Data shall consider Your Contributions for addition to the
official Redpanda Data open source project(s) for which they were
submitted. Except for the license granted herein to Redpanda Data and
recipients of software distributed by Redpanda Data, You reserve all right,
title, and interest in and to Your Contributions.

1\. Definitions.

“You” (or “Your”) shall mean the copyright owner or legal entity
authorized by the copyright owner that is entering into this CLA with
Redpanda Data. For legal entities, the entity making a Contribution and all
other entities that control, are controlled by, or are under common
control with that entity are considered to be a single Contributor. For
the purposes of this definition, “control” means (i) the power, direct
or indirect, to cause the direction or management of such entity,
whether by contract or otherwise, or (ii) ownership of fifty percent
(50%) or more of the outstanding shares, or (iii) beneficial ownership
of such entity.

“Contribution” shall mean any code, documentation or other original
works of authorship, including any modifications or additions to an
existing work, that are intentionally submitted by You to Redpanda Data for
inclusion in, or documentation of, any of the products owned or managed
by Redpanda Data (the “Work”). For the purposes of this definition,
“submitted” means any form of electronic, verbal, or written
communication sent to Redpanda Data or its representatives, including but
not limited to communication on electronic mailing lists, source code
control systems, and issue tracking systems that are managed by, or on
behalf of, Redpanda Data for the purpose of discussing and improving the
Work, but excluding communication that is conspicuously marked or
otherwise designated in writing by You as “Not a Contribution.”

2\. Grant of Copyright License. Subject to the terms and conditions of
this CLA, You hereby grant to Redpanda Data and to recipients of software
distributed by Redpanda Data a perpetual, worldwide, non-exclusive,
no-charge, royalty-free, irrevocable copyright license to reproduce,
prepare derivative works of, publicly display, publicly perform,
sublicense, and distribute Your Contributions and such derivative works.

3\. Grant of Patent License. Subject to the terms and conditions of this
CLA, You hereby grant to Redpanda Data and to recipients of software
distributed by Redpanda Data a perpetual, worldwide, non-exclusive,
no-charge, royalty-free, irrevocable (except as stated in this section)
patent license to make, have made, use, offer to sell, sell, import, and
otherwise transfer the Work, where such license applies only to those
patent claims licensable by You that are necessarily infringed by Your
Contribution(s) alone or by combination of Your Contribution(s) with the
Work to which such Contribution(s) were submitted. If any entity
institutes patent litigation against You or any other entity (including
a cross-claim or counterclaim in a lawsuit) alleging that Your
Contribution, or the Work to which You have contributed, constitutes
direct or contributory patent infringement, then any patent licenses
granted to that entity under this CLA for that Contribution or Work
shall terminate as of the date such litigation is filed.

4\. Authority. You represent and warrant that You are legally entitled
to grant the above license. If You are an individual and Your
employer(s) has rights to intellectual property that You create that
includes Your Contributions, You represent that You have received
permission to make Contributions on behalf of that employer, that Your
employer has waived such rights for Your Contributions to Redpanda Data, or
that Your employer has entered into a separate CLA with Redpanda Data
covering Your Contributions. If You are a Company, You represent further
that each employee making a Contribution to Redpanda Data under the
Company’s name is authorized to submit Contributions on behalf of the
Company.

5\. Original Works. You represent and warrant that each of Your
Contributions is Your original creation (see section 7 for submissions
on behalf of others). You represent and warrant that, to Your knowledge,
none of Your Contributions infringe, violate, or misappropriate any
third party intellectual property or other proprietary rights.

6\. Disclaimer. You are not expected to provide support for Your
Contributions, except to the extent You desire to provide support. You
may provide support for free, for a fee, or not at all. UNLESS REQUIRED
BY APPLICABLE LAW OR AGREED TO IN WRITING, EXCEPT FOR THE WARRANTIES SET
FORTH ABOVE, YOU PROVIDE YOUR CONTRIBUTIONS ON AN “AS IS” BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, EITHER EXPRESS OR IMPLIED,
INCLUDING, WITHOUT LIMITATION, ANY WARRANTIES OR CONDITIONS OF TITLE,
NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.

7\. Submissions on Behalf of Others. Should You wish to submit work that
is not Your original creation, You may submit it to Redpanda Data
separately from any Contribution, identifying the complete details of
its source and of any license or other restriction (including, but not
limited to, related patents, trademarks, and license agreements) of
which You are personally aware, and conspicuously marking the work as
“Submitted on behalf of a third-party: \[name here\]”.

8\. Additional Facts/Circumstances. You agree to notify Redpanda Data of
any facts or circumstances of which You become aware that would make the
above representations and warranties inaccurate in any respect.

9\. Authorization. If You are entering into this CLA as a Company, You
represent and warrant that the individual accepting this CLA is duly
authorized to enter into this CLA on the Company’s behalf.

\[Field for Copyright Notice from Contributor, Inc. Name & (if
applicable) Company\]

\[ACCEPT\]

```

## legal/licenses/rcl.md

```text
**Redpanda Community License Agreement**

Please read this Redpanda Community License Agreement (the “Agreement”)
carefully before using the Software (as defined below), which is offered by
Redpanda Data, Inc. or its affiliated Legal Entities (“Redpanda Data”).

By downloading the Software or using it in any manner, You agree that You
have read and agree to be bound by the terms of this Agreement. If You
are accessing the Software on behalf of a Legal Entity, You represent and
warrant that You have the authority to agree to these terms on its
behalf and the right to bind that Legal Entity to this Agreement. Use of
the Software is expressly conditioned upon Your assent to all the terms of
this Agreement, to the exclusion of all other terms.

1.  **<span class="smallcaps">Definitions</span>.** In addition to other
    terms defined elsewhere in this Agreement, the terms below have the
    following meanings.

(a) “Software” shall mean an offering provided by Redpanda Data that references this Agreement as a governing license, including both the Community Edition and the Enterprise Edition, as defined below.

(b) “Community Edition” shall mean the version of Software available free of charge at a repository located at https://github.com/redpanda-data/ that references this Agreement as a governing license, which does not include the Enterprise Edition.

(c) “Enterprise Edition” shall mean the additional features made available by Redpanda Data, the use of which is subject to additional terms set out below.

(d) “Contribution” shall mean any work of authorship, including the original version of the Work and any modifications or additions to that Work or Derivative Works thereof, that is intentionally submitted Redpanda Data for inclusion in the Work by the copyright owner or by an individual or Legal Entity authorized to submit on behalf of the copyright owner. For the purposes of this definition, “submitted” means any form of electronic, verbal, or written communication sent to Redpanda Data or its representatives, including but not limited to communication on electronic mailing lists, source code control systems, and issue tracking systems that are managed by, or on behalf of, Redpanda Data for the purpose of discussing and improving the Work, but excluding communication that is conspicuously marked or otherwise designated in writing by the copyright owner as “Not a Contribution.”

(e) “Contributor” shall mean any copyright owner or individual or Legal Entity authorized by the copyright owner, other than Redpanda Data, from whom Redpanda Data receives a Contribution that Redpanda Data subsequently incorporates within the Work.

(f) “Derivative Works” shall mean any work, whether in Source or Object form, that is based on (or derived from) the Work, such as a translation, abridgement, condensation, or any other recasting, transformation, or adaptation for which the editorial revisions, annotations, elaborations, or other modifications represent, as a whole, an original work of authorship. For the purposes of this License, Derivative Works shall not include works that remain separable from, or merely link (or bind by name) to the interfaces of, the Work and Derivative Works thereof.

(g) “Legal Entity” shall mean the union of the acting entity and all other entities that control, are controlled by, or are under common control with that entity. For the purposes of this definition, “control” means (i) the power, direct or indirect, to cause the direction or management of such entity, whether by contract or otherwise, or (ii) ownership of fifty percent (50%) or more of the outstanding shares, or (iii) beneficial ownership of such entity.

(h) “License” shall mean the terms and conditions for use, reproduction, and distribution of a Work as defined by this Agreement.

(i) “Licensor” shall mean Redpanda Data or a Contributor, as applicable.

(j) “Object” form shall mean any form resulting from mechanical transformation or translation of a Source form, including but not limited to compiled object code, generated documentation, and conversions to other media types.

(k) “Source” form shall mean the preferred form for making modifications, including but not limited to software source code, documentation source, and configuration files.

(l) “Third Party Works” shall mean Works, including Contributions, and other technology owned by a person or Legal Entity other than Redpanda Data, as indicated by a copyright notice that is included in or attached to such Works or technology.

(m) “Work” shall mean the work of authorship, whether in Source or Object form, made available under a License, as indicated by a copyright notice that is included in or attached to the work.

(n) “You” (or “Your”) shall mean an individual or Legal Entity exercising permissions granted by this License.

2.  **<span class="smallcaps">Licenses</span>**.

1.  **License to Community Edition.** The License for Community Edition is, as applicable, 
        the Business Source License v.1.1 (please see
        the text of such license here (bsl.md) for full terms), or such other license referenced in the relevant repository
        Community Edition is a no-cost, entry-level license and as such,
        contains the following disclaimers: TO THE EXTENT PERMITTED BY
        APPLICABLE LAW, COMMUNITY EDITION IS PROVIDED ON AN “AS IS” BASIS.
        LICENSOR HEREBY DISCLAIMS ALL WARRANTIES AND CONDITIONS, EXPRESS
        OR IMPLIED, INCLUDING (WITHOUT LIMITATION) WARRANTIES OF
        MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE,
        NON-INFRINGEMENT, AND TITLE. For clarity, the terms of this
        Agreement, other than the relevant definitions in Section 1 and
        this Section 2(a) do not apply to Community Edition.

ii.   **License to Enterprise Edition.**

        a.  ***Grant of Copyright License:*** Subject to the terms of
            this Agreement, Licensor hereby grants to You a worldwide,
            non-exclusive, non-transferable limited license to
            reproduce, prepare Enterprise Derivative Works (as defined
            below) of, publicly display, publicly perform, sublicense,
            and distribute Enterprise Edition for Your business
            purposes, for so long as You are not in violation of this
            Section 2(b) and are current on all payments required by
            Section 4 below.

        b.  ***Grant of Patent License:*** Subject to the terms of this
            Agreement, Licensor hereby grants to You a worldwide,
            non-exclusive, non-transferable limited patent license to
            make, have made, use, offer to sell, sell, import, and
            otherwise transfer Enterprise Edition, where such
            license applies only to those patent claims licensable by
            Licensor that are necessarily infringed by their
            Contribution(s) alone or by combination of their
            Contribution(s) with the Work to which such Contribution(s)
            was submitted. If You institute patent litigation against
            any entity (including a cross-claim or counterclaim in a
            lawsuit) alleging that the Work or a Contribution
            incorporated within the Work constitutes direct or
            contributory patent infringement, then any patent licenses
            granted to You under this License for that Work shall
            terminate as of the date such litigation is filed.

        c.  ***License to Third Party Works:*** From time to time
            Redpanda Data may use, or provide You access to, Third Party
            Works in connection Enterprise Edition. You
            acknowledge and agree that in addition to this Agreement,
            Your use of Third Party Works is subject to all other terms
            and conditions set forth in the License provided with or
            contained in such Third Party Works. Some Third Party Works
            may be licensed to You solely for use with 
            Enterprise Edition under the terms of a third party License,
            or as otherwise notified by Redpanda Data, and not under the
            terms of this Agreement. You agree that the owners and third
            party licensors of Third Party Works are intended third
            party beneficiaries to this Agreement.

        d.  ***Use Restriction:*** You may make use of 
            Enterprise Edition, provided that you may not use 
            Enterprise Edition for a Streaming or Queuing Service. A
            “Streaming or Queueing Service” is a commercial offering
            that allows third parties (other than your employees and
            individual contractors) to access the functionality of
            Enterprise Edition by performing an action directly
            or indirectly that causes the creation of a topic in the
            Work. For clarity, a Streaming or Queuing Service would
            include providers of infrastructure services, such as cloud
            services, hosting services, data center services and
            similarly situated third parties (including affiliates of
            such entities) that would offer Enterprise Edition
            in connection with a broader service offering to customers
            or subscribers of such of such third party’s core services.

3.  **<span class="smallcaps">Support</span>.** From time to time, in
    its sole discretion, Redpanda Data may offer professional services or
    support for the Software, which may now or in the future be subject to
    additional fees.

4.  **<span class="smallcaps">Fees for Enterprise Edition or
    Support.</span>**

    i.  **Fees.** The License to Enterprise Edition is
        conditioned upon Your payment of the fees specified on
        [pricing](https://redpanda.com/contact), or as otherwise agreed to by Redpanda Data which You agree to pay to Redpanda Data in accordance
        with the payment terms set out on that page or as otherwise agreed to by Redpanda Data. Any professional
        services or support for Software may also be subject to Your
        payment of fees, which will be specified by Redpanda Data when you
        sign up to receive such professional services or support.
        Redpanda Data reserves the right to change the fees at any time
        with prior written notice; for recurring fees, any such
        adjustments will take effect as of the next payment period.

    ii.  **Overdue Payments and Taxes.** Overdue payments are subject to
        a service charge equal to the lesser of 1.5% per month or the
        maximum legal interest rate allowed by law, and You shall pay
        all Redpanda Data’s reasonable costs of collection, including court
        costs and attorneys’ fees. Fees are stated and payable in U.S.
        dollars and are exclusive of all sales, use, value added and
        similar taxes, duties, withholdings and other governmental
        assessments (but excluding taxes based on Redpanda Data’s income)
        that may be levied on the transactions contemplated by this
        Agreement in any jurisdiction, all of which are Your
        responsibility unless you have provided Redpanda Data with a valid
        tax-exempt certificate.

    iii.  **Record-keeping and Audit.** If fees for Enterprise
        Edition are based on the number of cores or servers running on
        Enterprise Edition or another use-based unit of
        measurement, You must maintain complete and accurate records
        with respect Your use of Enterprise Edition and will
        provide such records to Redpanda Data for inspection or audit upon
        Redpanda Data’s reasonable request. If an inspection or audit
        uncovers additional usage by You for which fees are owed under
        this Agreement, then You shall pay for such additional usage at
        Redpanda Data’s then-current rates. 

5.  **<span class="smallcaps">Trial License.</span>** If You have signed
    up for a trial or evaluation of Enterprise Edition, Your
    License to Enterprise Edition is granted without charge for
    the trial or evaluation period specified when You signed up, or if
    no term was specified, for thirty (30) calendar days, provided that
    Your License is granted solely for purposes of Your internal
    evaluation of Enterprise Edition during the trial or
    evaluation period (a “Trial License”). You may not use 
    Enterprise Edition under a Trial License more than once in any
    twelve (12) month period. Redpanda Data may revoke a Trial License at
    any time and for any reason. Sections 3, 4, 9 and 11 of this
    Agreement do not apply to Trial Licenses.

6.  **<span class="smallcaps">Redistribution.</span>** You may reproduce
    and distribute copies of the Work or Derivative Works thereof in any
    medium, with or without modifications, and in Source or Object form,
    provided that You meet the following conditions:

    i.  You must give any other recipients of the Work or Derivative
        Works a copy of this License; and

    ii.  You must cause any modified files to carry prominent notices
        stating that You changed the files; and

    iii.  You must retain, in the Source form of any Derivative Works that
        You distribute, all copyright, patent, trademark, and
        attribution notices from the Source form of the Work, excluding
        those notices that do not pertain to any part of the Derivative
        Works; and

    iv.  If the Work includes a “NOTICE” text file as part of its
        distribution, then any Derivative Works that You distribute must
        include a readable copy of the attribution notices contained
        within such NOTICE file, excluding those notices that do not
        pertain to any part of the Derivative Works, in at least one of
        the following places: within a NOTICE text file distributed as
        part of the Derivative Works; within the Source form or
        documentation, if provided along with the Derivative Works; or,
        within a display generated by the Derivative Works, if and
        wherever such third-party notices normally appear. The contents
        of the NOTICE file are for informational purposes only and do
        not modify the License. You may add Your own attribution notices
        within Derivative Works that You distribute, alongside or as an
        addendum to the NOTICE text from the Work, provided that such
        additional attribution notices cannot be construed as modifying
        the License.

    v.  You may add Your own copyright statement to Your modifications
        and may provide additional or different license terms and
        conditions for use, reproduction, or distribution of Your
        modifications, or for any such Derivative Works as a whole,
        provided Your use, reproduction, and distribution of the Work
        otherwise complies with the conditions stated in this License.

    6.  **Enterprise Derivative Works.** Derivative Works of 
        Enterprise Edition (“Enterprise Derivative Works”) may be made,
        reproduced and distributed in any medium, with or without
        modifications, in Source or Object form, provided that each
        Enterprise Derivative Work will be considered to include a
        License to Enterprise Edition and thus will be subject
        to the payment of fees to Redpanda Data by any user of the
        Enterprise Derivative Work.

7.  **<span class="smallcaps">Submission of Contributions.</span>**
    Unless You explicitly state otherwise, any Contribution
    intentionally submitted for inclusion in the Software by You to
    Redpanda Data shall be under the terms and conditions of
    [https://cla-assistant.io/redpanda-data/redpanda] (which is based off of the
    Apache License) or such other terms referenced in the relevant repository, without any additional terms or conditions,
    payments of royalties or otherwise to Your benefit. Notwithstanding
    the above, nothing herein shall supersede or modify the terms of any
    separate license agreement You may have executed with Redpanda Data
    regarding such Contributions.

8.  **<span class="smallcaps">Trademarks.</span>** This License does not
    grant permission to use the trade names, trademarks, service marks,
    or product names of Licensor, except as required for reasonable and
    customary use in describing the origin of the Work and reproducing
    the content of the NOTICE file.

9.  **<span class="smallcaps">Limited Warranty.</span>**

    1.  **Warranties.** Redpanda Data warrants to You that: (i) 
        Enterprise Edition will materially perform in accordance with
        the applicable documentation for ninety (90) days after initial
        delivery to You; and (ii) any professional services performed by
        Redpanda Data under this Agreement will be performed in a
        workmanlike manner, in accordance with general industry
        standards.

    2.  **Exclusions.** Redpanda Data’s warranties in this Section 9 do not
        extend to problems that result from: (i) Your failure to
        implement updates issued by Redpanda Data during the warranty
        period; (ii) any alterations or additions (including Enterprise
        Derivative Works and Contributions) to the Software not performed by
        or at the direction of Redpanda Data; (iii) failures that are not
        reproducible by Redpanda Data; (iv) operation of 
        Enterprise Edition in violation of this Agreement or not in
        accordance with its documentation; (v) failures caused by
        software, hardware or products not licensed or provided by
        Redpanda Data hereunder; or (vi) Third Party Works.

    3.  **Remedies.** In the event of a breach of a warranty under this
        Section 9, Redpanda Data will, at its discretion and cost, either
        repair, replace or re-perform the applicable Works or services
        or refund a portion of fees previously paid to Redpanda Data that
        are associated with the defective Works or services. This is
        Your exclusive remedy, and Redpanda Data’s sole liability, arising
        in connection with the limited warranties herein.

10.  **<span class="smallcaps">Disclaimer of Warranty.</span>** EXCEPT AS
    SET OUT IN SECTION 9, UNLESS REQUIRED BY APPLICABLE LAW, LICENSOR
    PROVIDES THE WORK (AND EACH CONTRIBUTOR PROVIDES ITS CONTRIBUTIONS)
    ON AN “AS IS” BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
    EITHER EXPRESS OR IMPLIED, ARISING OUT OF COURSE OF DEALING, COURSE
    OF PERFORMANCE, OR USAGE IN TRADE, INCLUDING, WITHOUT LIMITATION,
    ANY WARRANTIES OR CONDITIONS OF TITLE, NON-INFRINGEMENT,
    MERCHANTABILITY, CORRECTNESS, RELIABILITY, OR FITNESS FOR A
    PARTICULAR PURPOSE, ALL OF WHICH ARE HEREBY DISCLAIMED. YOU ARE
    SOLELY RESPONSIBLE FOR DETERMINING THE APPROPRIATENESS OF USING OR
    REDISTRIBUTING WORKS AND ASSUME ANY RISKS ASSOCIATED WITH YOUR
    EXERCISE OF PERMISSIONS UNDER THE APPLICABLE LICENSE FOR SUCH WORKS.

11. **<span class="smallcaps">Limited Indemnity.</span>**

    1.  **Indemnity.** Redpanda Data will defend, indemnify and hold You
        harmless against any third party claims, liabilities or expenses
        incurred (including reasonable attorneys’ fees), as well as
        amounts finally awarded in a settlement or a non-appealable
        judgement by a court (“Losses”), to the extent arising from any
        claim or allegation by a third party that Enterprise
        Edition infringes or misappropriates a valid United States
        patent, copyright or trade secret right of a third party;
        provided that You give Redpanda Data: (i) prompt written notice of
        any such claim or allegation; (ii) sole control of the defense
        and settlement thereof; and (iii) reasonable cooperation and
        assistance in such defense or settlement. If any Work within
        Enterprise Edition becomes or, in Redpanda Data’s opinion,
        is likely to become, the subject of an injunction, Redpanda Data
        may, at its option, (A) procure for You the right to continue
        using such Work, (B) replace or modify such Work so that it
        becomes non-infringing without substantially compromising its
        functionality, or, if (A) and (B) are not commercially
        practicable, then (C) terminate Your license to the allegedly
        infringing Work and refund to You a prorated portion of the
        prepaid and unearned fees for such infringing Work. The
        foregoing states the entire liability of Redpanda Data with respect
        to infringement of patents, copyrights, trade secrets or other
        intellectual property rights.

    2.  **Exclusions.** The foregoing obligations shall not apply
        to: (i) Works modified by any party other than Redpanda Data
        (including Enterprise Derivative Works and Contributions), if
        the alleged infringement relates to such modification, (ii)
        Works combined or bundled with any products, processes or
        materials not provided by Redpanda Data where the alleged
        infringement relates to such combination, (iii) use of a version
        of Enterprise Edition other than the version that was
        current at the time of such use, as long as a non-infringing
        version had been released, (iv) any Works created to Your
        specifications, (v) infringement or misappropriation of any
        proprietary right in which You have an interest, or (vi) Third
        Party Works. You will defend, indemnify and hold Redpanda Data
        harmless against any Losses arising from any such claim or
        allegation, subject to conditions reciprocal to those in Section
        11(a).

12. **<span class="smallcaps">Limitation of Liability.</span>** In no
    event and under no legal or equitable theory, whether in tort
    (including negligence), contract, or otherwise, unless required by
    applicable law (such as deliberate and grossly negligent acts), and
    notwithstanding anything in this Agreement to the contrary, shall
    Licensor or any Contributor be liable to You for (i) any amounts in
    excess, in the aggregate, of the fees paid by You to Redpanda Data
    under this Agreement in the twelve (12) months preceding the date
    the first cause of liability arose), or (ii) any indirect, special,
    incidental, punitive, exemplary, reliance, or consequential damages
    of any character arising as a result of this Agreement or out of the
    use or inability to use the Work (including but not limited to
    damages for loss of goodwill, profits, data or data use, work
    stoppage, computer failure or malfunction, cost of procurement of
    substitute goods, technology or services, or any and all other
    commercial damages or losses), even if such Licensor or Contributor
    has been advised of the possibility of such damages. THESE
    LIMITATIONS SHALL APPLY NOTWITHSTANDING THE FAILURE OF THE ESSENTIAL
    PURPOSE OF ANY LIMITED REMEDY.

13. **<span class="smallcaps">Accepting Warranty or Additional
    Liability.</span>** While redistributing Works or Derivative Works
    thereof, and without limiting your obligations under Section 6, You
    may choose to offer, and charge a fee for, acceptance of support,
    warranty, indemnity, or other liability obligations and/or rights
    consistent with this License. However, in accepting such
    obligations, You may act only on Your own behalf and on Your sole
    responsibility, not on behalf of any other Contributor, and only if
    You agree to indemnify, defend, and hold Redpanda Data and each other
    Contributor harmless for any liability incurred by, or claims
    asserted against, such Contributor by reason of your accepting any
    such warranty or additional liability.

14.  Operational and Usage Data: You acknowledge and agree that the Software may share data generated from the usage, configuration, deployment, access, or performance of Software, which may include contact information (such data, the “Operational and Usage Data”), with Redpanda Data. Any disclosure or use of Operational and Usage Data shall be subject to, and in accordance with, Redpanda Data’s Privacy Policy found at https://www.redpanda.com/legal/privacy-policy. For the avoidance of doubt, Operational and Usage Data does not include, and is not derived from data submitted into the Software by You.
15. **<span class="smallcaps">General.</span>**

    i.  **Relationship of Parties.** You and Redpanda Data are independent
        contractors, and nothing herein shall be deemed to constitute
        either party as the agent or representative of the other or both
        parties as joint venturers or partners for any purpose.

    ii.  **Export Control.** You shall comply with the U.S. Foreign
        Corrupt Practices Act and all applicable export laws,
        restrictions and regulations of the U.S. Department of Commerce,
        and any other applicable U.S. and foreign authority.

    iii.  **Assignment.** This Agreement and the rights and obligations
        herein may not be assigned or transferred, in whole or in part,
        by You without the prior written consent of Redpanda Data. Any
        assignment in violation of this provision is void. This
        Agreement shall be binding upon, and inure to the benefit of,
        the successors and permitted assigns of the parties.

    iv.  **Governing Law.** This Agreement shall be governed by and
        construed under the laws of the State of California and the
        United States without regard to conflicts of laws provisions
        thereof, and without regard to the Uniform Computer Information
        Transactions Act.

    v.  **Attorneys’ Fees.** In any action or proceeding to enforce
        rights under this Agreement, the prevailing party shall be
        entitled to recover its costs, expenses and attorneys’ fees.

    vi.  **Severability.** If any provision of this Agreement is held to
        be invalid, illegal or unenforceable in any respect, that
        provision shall be limited or eliminated to the minimum extent
        necessary so that this Agreement otherwise remains in full force
        and effect and enforceable.

    vii.  **Entire Agreement; Waivers; Modification.** This Agreement
        constitutes the entire agreement between the parties relating to
        the subject matter hereof and supersedes all proposals,
        understandings, or discussions, whether written or oral,
        relating to the subject matter of this Agreement and all past
        dealing or industry custom. The failure of either party to
        enforce its rights under this Agreement at any time for any
        period shall not be construed as a waiver of such rights. No
        changes, modifications or waivers to this Agreement will be
        effective unless in writing and signed by both parties.

```

## legal/licenses/rcl_header.go.txt

```text
// Copyright 2024 Redpanda Data, Inc.
//
// Licensed as a Redpanda Enterprise file under the Redpanda Community
// License (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
//
// https://github.com/redpanda-data/connect/blob/main/licenses/rcl.md


```

## legal/licenses/third_party.md

```text
# Licenses

| Software | License |
| :------- | :------ |
| cel.dev/expr | Apache-2.0 |
| cloud.google.com/go | Apache-2.0 |
| cloud.google.com/go/aiplatform | Apache-2.0 |
| cloud.google.com/go/auth | Apache-2.0 |
| cloud.google.com/go/auth/oauth2adapt | Apache-2.0 |
| cloud.google.com/go/bigquery | Apache-2.0 |
| cloud.google.com/go/compute/metadata | Apache-2.0 |
| cloud.google.com/go/iam | Apache-2.0 |
| cloud.google.com/go/longrunning | Apache-2.0 |
| cloud.google.com/go/monitoring | Apache-2.0 |
| cloud.google.com/go/pubsub | Apache-2.0 |
| cloud.google.com/go/secretmanager | Apache-2.0 |
| cloud.google.com/go/spanner | Apache-2.0 |
| cloud.google.com/go/storage | Apache-2.0 |
| cloud.google.com/go/trace | Apache-2.0 |
| cloud.google.com/go/vertexai/genai | Apache-2.0 |
| cloud.google.com/go/vertexai/internal | Apache-2.0 |
| cuelang.org/go | Apache-2.0 |
| filippo.io/edwards25519 | BSD-3-Clause |
| github.com/99designs/go-keychain | MIT |
| github.com/99designs/keyring | MIT |
| github.com/AthenZ/athenz | Apache-2.0 |
| github.com/Azure/azure-sdk-for-go/sdk/azcore | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/azidentity | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/data/azcosmos | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/data/aztables | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/internal | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/keyvault/azsecrets | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/keyvault/internal | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/storage/azblob | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/storage/azdatalake | MIT |
| github.com/Azure/azure-sdk-for-go/sdk/storage/azqueue | MIT |
| github.com/Azure/go-amqp | MIT |
| github.com/AzureAD/microsoft-authentication-library-for-go/apps | MIT |
| github.com/ClickHouse/ch-go | Apache-2.0 |
| github.com/ClickHouse/clickhouse-go/v2 | Apache-2.0 |
| github.com/DataDog/zstd | BSD-3-Clause |
| github.com/GoogleCloudPlatform/grpc-gcp-go/grpcgcp | Apache-2.0 |
| github.com/GoogleCloudPlatform/opentelemetry-operations-go/detectors/gcp | Apache-2.0 |
| github.com/GoogleCloudPlatform/opentelemetry-operations-go/exporter/trace | Apache-2.0 |
| github.com/GoogleCloudPlatform/opentelemetry-operations-go/internal/resourcemapping | Apache-2.0 |
| github.com/IBM/sarama | MIT |
| github.com/Jeffail/checkpoint | MIT |
| github.com/Jeffail/gabs/v2 | MIT |
| github.com/Jeffail/grok | Apache-2.0 |
| github.com/Jeffail/shutdown | MIT |
| github.com/JohnCGriffin/overflow | MIT |
| github.com/Masterminds/squirrel | MIT |
| github.com/OneOfOne/xxhash | Apache-2.0 |
| github.com/PaesslerAG/gval | BSD-3-Clause |
| github.com/PaesslerAG/jsonpath | BSD-3-Clause |
| github.com/andybalholm/brotli | MIT |
| github.com/apache/arrow/go/arrow | Apache-2.0 |
| github.com/apache/arrow/go/v15 | Apache-2.0 |
| github.com/apache/pulsar-client-go | Apache-2.0 |
| github.com/apache/thrift/lib/go/thrift | Apache-2.0 |
| github.com/apapsch/go-jsonmerge/v2 | MIT |
| github.com/ardielle/ardielle-go/rdl | Apache-2.0 |
| github.com/authzed/authzed-go | Apache-2.0 |
| github.com/authzed/grpcutil | Apache-2.0 |
| github.com/aws/aws-lambda-go/lambda | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2 | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/config | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/credentials | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/feature/dynamodb/expression | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/feature/ec2/imds | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/internal/configsources | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/internal/endpoints/v2 | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/internal/ini | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/internal/sync/singleflight | BSD-3-Clause |
| github.com/aws/aws-sdk-go-v2/internal/v4a | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/bedrockruntime | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/cloudwatch | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/dynamodb | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/dynamodbstreams/types | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/firehose | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/internal/accept-encoding | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/internal/checksum | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/internal/endpoint-discovery | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/internal/presigned-url | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/internal/s3shared | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/kinesis | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/lambda | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/s3 | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/secretsmanager | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/sns | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/sqs | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/sso | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/ssooidc | Apache-2.0 |
| github.com/aws/aws-sdk-go-v2/service/sts | Apache-2.0 |
| github.com/aws/smithy-go | Apache-2.0 |
| github.com/aws/smithy-go/internal/sync/singleflight | BSD-3-Clause |
| github.com/aymerick/douceur | MIT |
| github.com/beanstalkd/go-beanstalk | MIT |
| github.com/benhoyt/goawk | MIT |
| github.com/beorn7/perks/quantile | MIT |
| github.com/bits-and-blooms/bitset | BSD-3-Clause |
| github.com/bradfitz/gomemcache/memcache | Apache-2.0 |
| github.com/btnguyen2k/consu/checksum | MIT |
| github.com/btnguyen2k/consu/g18 | MIT |
| github.com/btnguyen2k/consu/gjrc | MIT |
| github.com/btnguyen2k/consu/olaf | MIT |
| github.com/btnguyen2k/consu/reddo | MIT |
| github.com/btnguyen2k/consu/semita | MIT |
| github.com/bufbuild/protocompile | Apache-2.0 |
| github.com/bwmarrin/discordgo | BSD-3-Clause |
| github.com/bwmarrin/snowflake | BSD-2-Clause |
| github.com/cenkalti/backoff/v4 | MIT |
| github.com/census-instrumentation/opencensus-proto/gen-go | Apache-2.0 |
| github.com/certifi/gocertifi | MPL-2.0 |
| github.com/cespare/xxhash/v2 | MIT |
| github.com/clbanning/mxj/v2 | MIT |
| github.com/cncf/xds/go | Apache-2.0 |
| github.com/cockroachdb/apd/v3 | Apache-2.0 |
| github.com/cohere-ai/cohere-go/v2 | MIT |
| github.com/colinmarc/hdfs | MIT |
| github.com/couchbase/gocb/v2 | Apache-2.0 |
| github.com/couchbase/gocbcore/v10 | Apache-2.0 |
| github.com/couchbase/gocbcoreps | Apache-2.0 |
| github.com/couchbaselabs/gocbconnstr/v2 | Apache-2.0 |
| github.com/cpuguy83/go-md2man/v2/md2man | MIT |
| github.com/cyborginc/cyborgdb-go | MIT |
| github.com/davecgh/go-spew/spew | ISC |
| github.com/denisenkom/go-mssqldb | BSD-3-Clause |
| github.com/dgraph-io/ristretto/v2 | Apache-2.0 |
| github.com/dgraph-io/ristretto/v2/z | MIT |
| github.com/dgryski/go-rendezvous | MIT |
| github.com/dlclark/regexp2 | MIT |
| github.com/dop251/goja | MIT |
| github.com/dop251/goja/ftoa/internal/fast | BSD-3-Clause |
| github.com/dop251/goja_nodejs | MIT |
| github.com/dustin/go-humanize | MIT |
| github.com/dvsekhvalnov/jose2go | MIT |
| github.com/eapache/go-resiliency/breaker | MIT |
| github.com/eapache/go-xerial-snappy | MIT |
| github.com/eapache/queue | MIT |
| github.com/eclipse/paho.mqtt.golang | EPL-2.0 |
| github.com/envoyproxy/go-control-plane/envoy | Apache-2.0 |
| github.com/envoyproxy/protoc-gen-validate/validate | Apache-2.0 |
| github.com/fatih/color | MIT |
| github.com/felixge/httpsnoop | MIT |
| github.com/fsnotify/fsnotify | BSD-3-Clause |
| github.com/gabriel-vasile/mimetype | MIT |
| github.com/generikvault/gvalstrings | BSD-3-Clause |
| github.com/getsentry/sentry-go | MIT |
| github.com/go-faker/faker/v4 | MIT |
| github.com/go-faster/city | MIT |
| github.com/go-faster/errors | BSD-3-Clause |
| github.com/go-jose/go-jose/v3 | Apache-2.0 |
| github.com/go-jose/go-jose/v3/json | BSD-3-Clause |
| github.com/go-logr/logr | Apache-2.0 |
| github.com/go-logr/stdr | Apache-2.0 |
| github.com/go-resty/resty/v2 | MIT |
| github.com/go-sourcemap/sourcemap | BSD-2-Clause |
| github.com/go-sql-driver/mysql | MPL-2.0 |
| github.com/goccy/go-json | MIT |
| github.com/gocql/gocql | BSD-3-Clause |
| github.com/gofrs/uuid | MIT |
| github.com/gogo/protobuf | BSD-3-Clause |
| github.com/golang-jwt/jwt | MIT |
| github.com/golang-jwt/jwt/v5 | MIT |
| github.com/golang-sql/civil | Apache-2.0 |
| github.com/golang-sql/sqlexp | BSD-3-Clause |
| github.com/golang/groupcache/lru | Apache-2.0 |
| github.com/golang/protobuf/proto | BSD-3-Clause |
| github.com/golang/snappy | BSD-3-Clause |
| github.com/google/flatbuffers/go | Apache-2.0 |
| github.com/google/pprof/profile | Apache-2.0 |
| github.com/google/s2a-go | Apache-2.0 |
| github.com/google/uuid | BSD-3-Clause |
| github.com/googleapis/enterprise-certificate-proxy/client | Apache-2.0 |
| github.com/googleapis/gax-go/v2 | BSD-3-Clause |
| github.com/googleapis/go-sql-spanner | Apache-2.0 |
| github.com/gorilla/css/scanner | BSD-3-Clause |
| github.com/gorilla/handlers | BSD-3-Clause |
| github.com/gorilla/mux | BSD-3-Clause |
| github.com/gorilla/websocket | BSD-2-Clause |
| github.com/gosimple/slug | MPL-2.0 |
| github.com/gosimple/unidecode | Apache-2.0 |
| github.com/govalues/decimal | MIT |
| github.com/grpc-ecosystem/go-grpc-middleware | Apache-2.0 |
| github.com/grpc-ecosystem/grpc-gateway/v2 | BSD-3-Clause |
| github.com/hailocab/go-hostpool | MIT |
| github.com/hamba/avro/v2 | MIT |
| github.com/hashicorp/errwrap | MPL-2.0 |
| github.com/hashicorp/go-multierror | MPL-2.0 |
| github.com/hashicorp/go-uuid | MPL-2.0 |
| github.com/hashicorp/golang-lru/arc/v2 | MPL-2.0 |
| github.com/hashicorp/golang-lru/v2 | MPL-2.0 |
| github.com/hashicorp/golang-lru/v2/simplelru | BSD-3-Clause |
| github.com/influxdata/go-syslog/v3 | MIT |
| github.com/influxdata/influxdb1-client | MIT |
| github.com/itchyny/gojq | MIT |
| github.com/itchyny/timefmt-go | MIT |
| github.com/jackc/chunkreader/v2 | MIT |
| github.com/jackc/pgconn | MIT |
| github.com/jackc/pgio | MIT |
| github.com/jackc/pgpassfile | MIT |
| github.com/jackc/pgproto3/v2 | MIT |
| github.com/jackc/pgservicefile | MIT |
| github.com/jackc/pgtype | MIT |
| github.com/jackc/pgx/v4 | MIT |
| github.com/jackc/pgx/v5 | MIT |
| github.com/jackc/puddle | MIT |
| github.com/jcmturner/aescts/v2 | Apache-2.0 |
| github.com/jcmturner/dnsutils/v2 | Apache-2.0 |
| github.com/jcmturner/gofork | BSD-3-Clause |
| github.com/jcmturner/gokrb5/v8 | Apache-2.0 |
| github.com/jcmturner/rpc/v2 | Apache-2.0 |
| github.com/jhump/protoreflect | Apache-2.0 |
| github.com/jmespath/go-jmespath | Apache-2.0 |
| github.com/josharian/intern | MIT |
| github.com/json-iterator/go | MIT |
| github.com/jzelinskie/stringz | Apache-2.0 |
| github.com/klauspost/compress | Apache-2.0 |
| github.com/klauspost/compress/internal/snapref | BSD-3-Clause |
| github.com/klauspost/compress/s2 | BSD-3-Clause |
| github.com/klauspost/compress/snappy | BSD-3-Clause |
| github.com/klauspost/compress/zstd/internal/xxhash | MIT |
| github.com/klauspost/pgzip | MIT |
| github.com/kr/fs | BSD-3-Clause |
| github.com/kylelemons/godebug | Apache-2.0 |
| github.com/lann/builder | MIT |
| github.com/lann/ps | MIT |
| github.com/lib/pq | MIT |
| github.com/linkedin/goavro/v2 | Apache-2.0 |
| github.com/mailru/easyjson | MIT |
| github.com/matoous/go-nanoid/v2 | MIT |
| github.com/mattn/go-colorable | MIT |
| github.com/mattn/go-isatty | MIT |
| github.com/mattn/go-runewidth | MIT |
| github.com/microcosm-cc/bluemonday | BSD-3-Clause |
| github.com/microsoft/gocosmos | MIT |
| github.com/mitchellh/mapstructure | MIT |
| github.com/modern-go/concurrent | Apache-2.0 |
| github.com/modern-go/reflect2 | Apache-2.0 |
| github.com/montanaflynn/stats | MIT |
| github.com/mtibben/percent | MIT |
| github.com/munnerz/goautoneg | BSD-3-Clause |
| github.com/nats-io/nats.go | Apache-2.0 |
| github.com/nats-io/nkeys | Apache-2.0 |
| github.com/nats-io/nuid | Apache-2.0 |
| github.com/nats-io/stan.go | Apache-2.0 |
| github.com/ncruces/go-strftime | MIT |
| github.com/neo4j/neo4j-go-driver/v5/neo4j | Apache-2.0 |
| github.com/nsf/jsondiff | MIT |
| github.com/nsqio/go-nsq | MIT |
| github.com/oapi-codegen/runtime | Apache-2.0 |
| github.com/oklog/ulid | Apache-2.0 |
| github.com/olekukonko/tablewriter | MIT |
| github.com/olivere/elastic/v7 | MIT |
| github.com/olivere/elastic/v7/uritemplates | MIT |
| github.com/ollama/ollama | MIT |
| github.com/opensearch-project/opensearch-go/v3 | Apache-2.0 |
| github.com/oschwald/geoip2-golang | ISC |
| github.com/oschwald/maxminddb-golang | ISC |
| github.com/parquet-go/parquet-go | Apache-2.0 |
| github.com/parquet-go/parquet-go/bloom/xxhash | MIT |
| github.com/paulmach/orb | MIT |
| github.com/pgvector/pgvector-go | MIT |
| github.com/pierrec/lz4 | BSD-3-Clause |
| github.com/pierrec/lz4/v4 | BSD-3-Clause |
| github.com/pinecone-io/go-pinecone | Apache-2.0 |
| github.com/pkg/browser | BSD-2-Clause |
| github.com/pkg/errors | BSD-2-Clause |
| github.com/pkg/sftp | BSD-2-Clause |
| github.com/planetscale/vtprotobuf | BSD-3-Clause |
| github.com/pmezard/go-difflib/difflib | BSD-3-Clause |
| github.com/prometheus/client_golang/prometheus | Apache-2.0 |
| github.com/prometheus/client_model/go | Apache-2.0 |
| github.com/prometheus/common | Apache-2.0 |
| github.com/prometheus/procfs | Apache-2.0 |
| github.com/pusher/pusher-http-go | MIT |
| github.com/qdrant/go-client/qdrant | Apache-2.0 |
| github.com/questdb/go-questdb-client/v4 | Apache-2.0 |
| github.com/quipo/dependencysolver | MIT |
| github.com/r3labs/diff/v3 | MPL-2.0 |
| github.com/rabbitmq/amqp091-go | BSD-2-Clause |
| github.com/rcrowley/go-metrics | BSD-2-Clause-FreeBSD |
| github.com/redis/go-redis/v9 | BSD-2-Clause |
| github.com/redpanda-data/benthos/v4 | MIT |
| github.com/remyoudompheng/bigfft | BSD-3-Clause |
| github.com/rickb777/period | BSD-3-Clause |
| github.com/rickb777/plural | BSD-3-Clause |
| github.com/rivo/uniseg | MIT |
| github.com/robfig/cron/v3 | MIT |
| github.com/rs/xid | MIT |
| github.com/russross/blackfriday/v2 | BSD-2-Clause |
| github.com/samber/lo | MIT |
| github.com/sashabaranov/go-openai | Apache-2.0 |
| github.com/segmentio/asm | MIT |
| github.com/segmentio/encoding/thrift | MIT |
| github.com/segmentio/ksuid | MIT |
| github.com/shopspring/decimal | MIT |
| github.com/sijms/go-ora/v2 | MIT |
| github.com/sirupsen/logrus | MIT |
| github.com/smira/go-statsd | MIT |
| github.com/snowflakedb/gosnowflake/v2 | Apache-2.0 |
| github.com/sourcegraph/conc | MIT |
| github.com/spaolacci/murmur3 | BSD-3-Clause |
| github.com/stretchr/testify | MIT |
| github.com/tetratelabs/wazero | Apache-2.0 |
| github.com/tidwall/gjson | MIT |
| github.com/tidwall/match | MIT |
| github.com/tidwall/pretty | MIT |
| github.com/tilinna/z85 | MIT |
| github.com/timeplus-io/proton-go-driver/v2 | Apache-2.0 |
| github.com/trinodb/trino-go-client/trino | Apache-2.0 |
| github.com/twmb/franz-go/pkg | BSD-3-Clause |
| github.com/twmb/franz-go/pkg/kadm | BSD-3-Clause |
| github.com/twmb/franz-go/pkg/kmsg | BSD-3-Clause |
| github.com/twmb/franz-go/pkg/sr | BSD-3-Clause |
| github.com/urfave/cli/v2 | MIT |
| github.com/vmihailenco/msgpack/v5 | BSD-2-Clause |
| github.com/vmihailenco/tagparser/v2 | BSD-2-Clause |
| github.com/xdg-go/pbkdf2 | Apache-2.0 |
| github.com/xdg-go/scram | Apache-2.0 |
| github.com/xdg-go/stringprep | Apache-2.0 |
| github.com/xeipuuv/gojsonpointer | Apache-2.0 |
| github.com/xeipuuv/gojsonreference | Apache-2.0 |
| github.com/xeipuuv/gojsonschema | Apache-2.0 |
| github.com/xitongsys/parquet-go | Apache-2.0 |
| github.com/xitongsys/parquet-go-source | Apache-2.0 |
| github.com/xrash/smetrics | MIT |
| github.com/youmark/pkcs8 | MIT |
| github.com/zeebo/xxh3 | BSD-2-Clause |
| go.mongodb.org/mongo-driver | Apache-2.0 |
| go.nanomsg.org/mangos/v3 | Apache-2.0 |
| go.opencensus.io | Apache-2.0 |
| go.opentelemetry.io/contrib/detectors/gcp | Apache-2.0 |
| go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc | Apache-2.0 |
| go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp | Apache-2.0 |
| go.opentelemetry.io/otel | Apache-2.0 |
| go.opentelemetry.io/otel/exporters/jaeger | Apache-2.0 |
| go.opentelemetry.io/otel/exporters/jaeger/internal/third_party/thrift/lib/go/thrift | Apache-2.0 |
| go.opentelemetry.io/otel/exporters/otlp/otlptrace | Apache-2.0 |
| go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc | Apache-2.0 |
| go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp | Apache-2.0 |
| go.opentelemetry.io/otel/metric | Apache-2.0 |
| go.opentelemetry.io/otel/sdk | Apache-2.0 |
| go.opentelemetry.io/otel/sdk/metric | Apache-2.0 |
| go.opentelemetry.io/otel/trace | Apache-2.0 |
| go.opentelemetry.io/proto/otlp | Apache-2.0 |
| go.uber.org/atomic | MIT |
| go.uber.org/multierr | MIT |
| go.uber.org/zap | MIT |
| golang.org/x/crypto | BSD-3-Clause |
| golang.org/x/exp | BSD-3-Clause |
| golang.org/x/mod/semver | BSD-3-Clause |
| golang.org/x/net | BSD-3-Clause |
| golang.org/x/oauth2 | BSD-3-Clause |
| golang.org/x/sync | BSD-3-Clause |
| golang.org/x/sys | BSD-3-Clause |
| golang.org/x/term | BSD-3-Clause |
| golang.org/x/text | BSD-3-Clause |
| golang.org/x/time/rate | BSD-3-Clause |
| golang.org/x/xerrors | BSD-3-Clause |
| google.golang.org/api | BSD-3-Clause |
| google.golang.org/api/internal/third_party/uritemplates | BSD-3-Clause |
| google.golang.org/genproto/googleapis | Apache-2.0 |
| google.golang.org/genproto/googleapis/api | Apache-2.0 |
| google.golang.org/genproto/googleapis/rpc | Apache-2.0 |
| google.golang.org/grpc | Apache-2.0 |
| google.golang.org/protobuf | BSD-3-Clause |
| gopkg.in/inf.v0 | BSD-3-Clause |
| gopkg.in/jcmturner/aescts.v1 | Apache-2.0 |
| gopkg.in/jcmturner/dnsutils.v1 | Apache-2.0 |
| gopkg.in/jcmturner/gokrb5.v6 | Apache-2.0 |
| gopkg.in/jcmturner/rpc.v1 | Apache-2.0 |
| gopkg.in/natefinch/lumberjack.v2 | MIT |
| gopkg.in/yaml.v3 | MIT |
| modernc.org/libc | BSD-3-Clause |
| modernc.org/libc/honnef.co/go/netdb | MIT |
| modernc.org/mathutil | Unknown |
| modernc.org/memory | BSD-3-Clause |
| modernc.org/sqlite | BSD-3-Clause |



```

## legal/public/bundle/enterprise/LICENSE

```text
**Redpanda Community License Agreement**

Please read this Redpanda Community License Agreement (the “Agreement”)
carefully before using Redpanda (as defined below), which is offered by
Redpanda Data, Inc. or its affiliated Legal Entities (“Redpanda Data”).

By downloading Redpanda or using it in any manner, You agree that You
have read and agree to be bound by the terms of this Agreement. If You
are accessing Redpanda on behalf of a Legal Entity, You represent and
warrant that You have the authority to agree to these terms on its
behalf and the right to bind that Legal Entity to this Agreement. Use of
Redpanda is expressly conditioned upon Your assent to all the terms of
this Agreement, to the exclusion of all other terms.

1.  **<span class="smallcaps">Definitions</span>.** In addition to other
    terms defined elsewhere in this Agreement, the terms below have the
    following meanings.

(a) “Redpanda” shall mean the event streaming platform provided by Redpanda Data, including both Redpanda Core and Redpanda Enterprise Edition, as defined below.

(b) “Redpanda Core” shall mean the version of Redpanda, available free of charge at https://github.com/redpanda-data/redpanda.

(c) “Redpanda Enterprise Edition” shall mean the additional features made available by Redpanda Data, the use of which is subject to additional terms set out below.

(d) “Contribution” shall mean any work of authorship, including the original version of the Work and any modifications or additions to that Work or Derivative Works thereof, that is intentionally submitted Redpanda Data for inclusion in the Work by the copyright owner or by an individual or Legal Entity authorized to submit on behalf of the copyright owner. For the purposes of this definition, “submitted” means any form of electronic, verbal, or written communication sent to Redpanda Data or its representatives, including but not limited to communication on electronic mailing lists, source code control systems, and issue tracking systems that are managed by, or on behalf of, Redpanda Data for the purpose of discussing and improving the Work, but excluding communication that is conspicuously marked or otherwise designated in writing by the copyright owner as “Not a Contribution.”

(e) “Contributor” shall mean any copyright owner or individual or Legal Entity authorized by the copyright owner, other than Redpanda Data, from whom Redpanda Data receives a Contribution that Redpanda Data subsequently incorporates within the Work.

(f) “Derivative Works” shall mean any work, whether in Source or Object form, that is based on (or derived from) the Work, such as a translation, abridgement, condensation, or any other recasting, transformation, or adaptation for which the editorial revisions, annotations, elaborations, or other modifications represent, as a whole, an original work of authorship. For the purposes of this License, Derivative Works shall not include works that remain separable from, or merely link (or bind by name) to the interfaces of, the Work and Derivative Works thereof.

(g) “Legal Entity” shall mean the union of the acting entity and all other entities that control, are controlled by, or are under common control with that entity. For the purposes of this definition, “control” means (i) the power, direct or indirect, to cause the direction or management of such entity, whether by contract or otherwise, or (ii) ownership of fifty percent (50%) or more of the outstanding shares, or (iii) beneficial ownership of such entity.

(h) “License” shall mean the terms and conditions for use, reproduction, and distribution of a Work as defined by this Agreement.

(i) “Licensor” shall mean Redpanda Data or a Contributor, as applicable.

(j) “Object” form shall mean any form resulting from mechanical transformation or translation of a Source form, including but not limited to compiled object code, generated documentation, and conversions to other media types.

(k) “Source” form shall mean the preferred form for making modifications, including but not limited to software source code, documentation source, and configuration files.

(l) “Third Party Works” shall mean Works, including Contributions, and other technology owned by a person or Legal Entity other than Redpanda Data, as indicated by a copyright notice that is included in or attached to such Works or technology.

(m) “Work” shall mean the work of authorship, whether in Source or Object form, made available under a License, as indicated by a copyright notice that is included in or attached to the work.

(n) “You” (or “Your”) shall mean an individual or Legal Entity exercising permissions granted by this License.

2.  **<span class="smallcaps">Licenses</span>**.

    1.  **License to Redpanda Core.** The License for Redpanda Core is
        the Business Source License v.1.1 ("BSL License"). Please see
        the text of the Redpanda [BSL License](bsl.md) for full terms.
        Redpanda Core is a no-cost, entry-level license and as such,
        contains the following disclaimers: TO THE EXTENT PERMITTED BY
        APPLICABLE LAW, REDPANDA CORE IS PROVIDED ON AN “AS IS” BASIS.
        LICENSOR HEREBY DISCLAIMS ALL WARRANTIES AND CONDITIONS, EXPRESS
        OR IMPLIED, INCLUDING (WITHOUT LIMITATION) WARRANTIES OF
        MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE,
        NON-INFRINGEMENT, AND TITLE. For clarity, the terms of this
        Agreement, other than the relevant definitions in Section 1 and
        this Section 2(a) do not apply to Redpanda Core.

    2.  **License to Redpanda Enterprise Edition.**

        1.  ***Grant of Copyright License:*** Subject to the terms of
            this Agreement, Licensor hereby grants to You a worldwide,
            non-exclusive, non-transferable limited license to
            reproduce, prepare Enterprise Derivative Works (as defined
            below) of, publicly display, publicly perform, sublicense,
            and distribute Redpanda Enterprise Edition for Your business
            purposes, for so long as You are not in violation of this
            Section 2(b) and are current on all payments required by
            Section 4 below.

        2.  ***Grant of Patent License:*** Subject to the terms of this
            Agreement, Licensor hereby grants to You a worldwide,
            non-exclusive, non-transferable limited patent license to
            make, have made, use, offer to sell, sell, import, and
            otherwise transfer Redpanda Enterprise Edition, where such
            license applies only to those patent claims licensable by
            Licensor that are necessarily infringed by their
            Contribution(s) alone or by combination of their
            Contribution(s) with the Work to which such Contribution(s)
            was submitted. If You institute patent litigation against
            any entity (including a cross-claim or counterclaim in a
            lawsuit) alleging that the Work or a Contribution
            incorporated within the Work constitutes direct or
            contributory patent infringement, then any patent licenses
            granted to You under this License for that Work shall
            terminate as of the date such litigation is filed.

        3.  ***License to Third Party Works:*** From time to time
            Redpanda Data may use, or provide You access to, Third Party
            Works in connection Redpanda Enterprise Edition. You
            acknowledge and agree that in addition to this Agreement,
            Your use of Third Party Works is subject to all other terms
            and conditions set forth in the License provided with or
            contained in such Third Party Works. Some Third Party Works
            may be licensed to You solely for use with Redpanda
            Enterprise Edition under the terms of a third party License,
            or as otherwise notified by Redpanda Data, and not under the
            terms of this Agreement. You agree that the owners and third
            party licensors of Third Party Works are intended third
            party beneficiaries to this Agreement.

        4.  ***Use Restriction:*** You may make use of Redpanda
            Enterprise Edition, provided that you may not use Redpanda
            Enterprise Edition for a Streaming or Queuing Service. A
            “Streaming or Queueing Service” is a commercial offering
            that allows third parties (other than your employees and
            individual contractors) to access the functionality of
            Redpanda Enterprise Edition by performing an action directly
            or indirectly that causes the creation of a topic in the
            Work. For clarity, a Streaming or Queuing Service would
            include providers of infrastructure services, such as cloud
            services, hosting services, data center services and
            similarly situated third parties (including affiliates of
            such entities) that would offer Redpanda Enterprise Edition
            in connection with a broader service offering to customers
            or subscribers of such of such third party’s core services.

3.  **<span class="smallcaps">Support</span>.** From time to time, in
    its sole discretion, Redpanda Data may offer professional services or
    support for Redpanda, which may now or in the future be subject to
    additional fees.

4.  **<span class="smallcaps">Fees for Redpanda Enterprise Edition or
    Redpanda Support.</span>**

    1.  **Fees.** The License to Redpanda Enterprise Edition is
        conditioned upon Your payment of the fees specified on
        [pricing](https://redpanda.com/contact) which You agree to pay to Redpanda Data in accordance
        with the payment terms set out on that page. Any professional
        services or support for Redpanda may also be subject to Your
        payment of fees, which will be specified by Redpanda Data when you
        sign up to receive such professional services or support.
        Redpanda Data reserves the right to change the fees at any time
        with prior written notice; for recurring fees, any such
        adjustments will take effect as of the next payment period.

    2.  **Overdue Payments and Taxes.** Overdue payments are subject to
        a service charge equal to the lesser of 1.5% per month or the
        maximum legal interest rate allowed by law, and You shall pay
        all Redpanda Data’s reasonable costs of collection, including court
        costs and attorneys’ fees. Fees are stated and payable in U.S.
        dollars and are exclusive of all sales, use, value added and
        similar taxes, duties, withholdings and other governmental
        assessments (but excluding taxes based on Redpanda Data’s income)
        that may be levied on the transactions contemplated by this
        Agreement in any jurisdiction, all of which are Your
        responsibility unless you have provided Redpanda Data with a valid
        tax-exempt certificate.

    3.  **Record-keeping and Audit.** If fees for Redpanda Enterprise
        Edition are based on the number of cores or servers running on
        Redpanda Enterprise Edition or another use-based unit of
        measurement, You must maintain complete and accurate records
        with respect Your use of Redpanda Enterprise Edition and will
        provide such records to Redpanda Data for inspection or audit upon
        Redpanda Data’s reasonable request. If an inspection or audit
        uncovers additional usage by You for which fees are owed under
        this Agreement, then You shall pay for such additional usage at
        Redpanda Data’s then-current rates.

5.  **<span class="smallcaps">Trial License.</span>** If You have signed
    up for a trial or evaluation of Redpanda Enterprise Edition, Your
    License to Redpanda Enterprise Edition is granted without charge for
    the trial or evaluation period specified when You signed up, or if
    no term was specified, for thirty (30) calendar days, provided that
    Your License is granted solely for purposes of Your internal
    evaluation of Redpanda Enterprise Edition during the trial or
    evaluation period (a “Trial License”). You may not use Redpanda
    Enterprise Edition under a Trial License more than once in any
    twelve (12) month period. Redpanda Data may revoke a Trial License at
    any time and for any reason. Sections 3, 4, 9 and 11 of this
    Agreement do not apply to Trial Licenses.

6.  **<span class="smallcaps">Redistribution.</span>** You may reproduce
    and distribute copies of the Work or Derivative Works thereof in any
    medium, with or without modifications, and in Source or Object form,
    provided that You meet the following conditions:

    1.  You must give any other recipients of the Work or Derivative
        Works a copy of this License; and

    2.  You must cause any modified files to carry prominent notices
        stating that You changed the files; and

    3.  You must retain, in the Source form of any Derivative Works that
        You distribute, all copyright, patent, trademark, and
        attribution notices from the Source form of the Work, excluding
        those notices that do not pertain to any part of the Derivative
        Works; and

    4.  If the Work includes a “NOTICE” text file as part of its
        distribution, then any Derivative Works that You distribute must
        include a readable copy of the attribution notices contained
        within such NOTICE file, excluding those notices that do not
        pertain to any part of the Derivative Works, in at least one of
        the following places: within a NOTICE text file distributed as
        part of the Derivative Works; within the Source form or
        documentation, if provided along with the Derivative Works; or,
        within a display generated by the Derivative Works, if and
        wherever such third-party notices normally appear. The contents
        of the NOTICE file are for informational purposes only and do
        not modify the License. You may add Your own attribution notices
        within Derivative Works that You distribute, alongside or as an
        addendum to the NOTICE text from the Work, provided that such
        additional attribution notices cannot be construed as modifying
        the License.

    5.  You may add Your own copyright statement to Your modifications
        and may provide additional or different license terms and
        conditions for use, reproduction, or distribution of Your
        modifications, or for any such Derivative Works as a whole,
        provided Your use, reproduction, and distribution of the Work
        otherwise complies with the conditions stated in this License.

    6.  **Enterprise Derivative Works.** Derivative Works of Redpanda
        Enterprise Edition (“Enterprise Derivative Works”) may be made,
        reproduced and distributed in any medium, with or without
        modifications, in Source or Object form, provided that each
        Enterprise Derivative Work will be considered to include a
        License to Redpanda Enterprise Edition and thus will be subject
        to the payment of fees to Redpanda Data by any user of the
        Enterprise Derivative Work.

7.  **<span class="smallcaps">Submission of Contributions.</span>**
    Unless You explicitly state otherwise, any Contribution
    intentionally submitted for inclusion in Redpanda by You to
    Redpanda Data shall be under the terms and conditions of
    [https://cla-assistant.io/redpanda-data/redpanda] (which is based off of the
    Apache License), without any additional terms or conditions,
    payments of royalties or otherwise to Your benefit. Notwithstanding
    the above, nothing herein shall supersede or modify the terms of any
    separate license agreement You may have executed with Redpanda Data
    regarding such Contributions.

8.  **<span class="smallcaps">Trademarks.</span>** This License does not
    grant permission to use the trade names, trademarks, service marks,
    or product names of Licensor, except as required for reasonable and
    customary use in describing the origin of the Work and reproducing
    the content of the NOTICE file.

9.  **<span class="smallcaps">Limited Warranty.</span>**

    1.  **Warranties.** Redpanda Data warrants to You that: (i) Redpanda
        Enterprise Edition will materially perform in accordance with
        the applicable documentation for ninety (90) days after initial
        delivery to You; and (ii) any professional services performed by
        Redpanda Data under this Agreement will be performed in a
        workmanlike manner, in accordance with general industry
        standards.

    2.  **Exclusions.** Redpanda Data’s warranties in this Section 9 do not
        extend to problems that result from: (i) Your failure to
        implement updates issued by Redpanda Data during the warranty
        period; (ii) any alterations or additions (including Enterprise
        Derivative Works and Contributions) to Redpanda not performed by
        or at the direction of Redpanda Data; (iii) failures that are not
        reproducible by Redpanda Data; (iv) operation of Redpanda
        Enterprise Edition in violation of this Agreement or not in
        accordance with its documentation; (v) failures caused by
        software, hardware or products not licensed or provided by
        Redpanda Data hereunder; or (vi) Third Party Works.

    3.  **Remedies.** In the event of a breach of a warranty under this
        Section 9, Redpanda Data will, at its discretion and cost, either
        repair, replace or re-perform the applicable Works or services
        or refund a portion of fees previously paid to Redpanda Data that
        are associated with the defective Works or services. This is
        Your exclusive remedy, and Redpanda Data’s sole liability, arising
        in connection with the limited warranties herein.

10.  **<span class="smallcaps">Disclaimer of Warranty.</span>** EXCEPT AS
    SET OUT IN SECTION 9, UNLESS REQUIRED BY APPLICABLE LAW, LICENSOR
    PROVIDES THE WORK (AND EACH CONTRIBUTOR PROVIDES ITS CONTRIBUTIONS)
    ON AN “AS IS” BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
    EITHER EXPRESS OR IMPLIED, ARISING OUT OF COURSE OF DEALING, COURSE
    OF PERFORMANCE, OR USAGE IN TRADE, INCLUDING, WITHOUT LIMITATION,
    ANY WARRANTIES OR CONDITIONS OF TITLE, NON-INFRINGEMENT,
    MERCHANTABILITY, CORRECTNESS, RELIABILITY, OR FITNESS FOR A
    PARTICULAR PURPOSE, ALL OF WHICH ARE HEREBY DISCLAIMED. YOU ARE
    SOLELY RESPONSIBLE FOR DETERMINING THE APPROPRIATENESS OF USING OR
    REDISTRIBUTING WORKS AND ASSUME ANY RISKS ASSOCIATED WITH YOUR
    EXERCISE OF PERMISSIONS UNDER THE APPLICABLE LICENSE FOR SUCH WORKS.

11. **<span class="smallcaps">Limited Indemnity.</span>**

    1.  **Indemnity.** Redpanda Data will defend, indemnify and hold You
        harmless against any third party claims, liabilities or expenses
        incurred (including reasonable attorneys’ fees), as well as
        amounts finally awarded in a settlement or a non-appealable
        judgement by a court (“Losses”), to the extent arising from any
        claim or allegation by a third party that Redpanda Enterprise
        Edition infringes or misappropriates a valid United States
        patent, copyright or trade secret right of a third party;
        provided that You give Redpanda Data: (i) prompt written notice of
        any such claim or allegation; (ii) sole control of the defense
        and settlement thereof; and (iii) reasonable cooperation and
        assistance in such defense or settlement. If any Work within
        Redpanda Enterprise Edition becomes or, in Redpanda Data’s opinion,
        is likely to become, the subject of an injunction, Redpanda Data
        may, at its option, (A) procure for You the right to continue
        using such Work, (B) replace or modify such Work so that it
        becomes non-infringing without substantially compromising its
        functionality, or, if (A) and (B) are not commercially
        practicable, then (C) terminate Your license to the allegedly
        infringing Work and refund to You a prorated portion of the
        prepaid and unearned fees for such infringing Work. The
        foregoing states the entire liability of Redpanda Data with respect
        to infringement of patents, copyrights, trade secrets or other
        intellectual property rights.

    2.  **Exclusions.** The foregoing obligations shall not apply
        to: (i) Works modified by any party other than Redpanda Data
        (including Enterprise Derivative Works and Contributions), if
        the alleged infringement relates to such modification, (ii)
        Works combined or bundled with any products, processes or
        materials not provided by Redpanda Data where the alleged
        infringement relates to such combination, (iii) use of a version
        of Redpanda Enterprise Edition other than the version that was
        current at the time of such use, as long as a non-infringing
        version had been released, (iv) any Works created to Your
        specifications, (v) infringement or misappropriation of any
        proprietary right in which You have an interest, or (vi) Third
        Party Works. You will defend, indemnify and hold Redpanda Data
        harmless against any Losses arising from any such claim or
        allegation, subject to conditions reciprocal to those in Section
        11(a).

12. **<span class="smallcaps">Limitation of Liability.</span>** In no
    event and under no legal or equitable theory, whether in tort
    (including negligence), contract, or otherwise, unless required by
    applicable law (such as deliberate and grossly negligent acts), and
    notwithstanding anything in this Agreement to the contrary, shall
    Licensor or any Contributor be liable to You for (i) any amounts in
    excess, in the aggregate, of the fees paid by You to Redpanda Data
    under this Agreement in the twelve (12) months preceding the date
    the first cause of liability arose), or (ii) any indirect, special,
    incidental, punitive, exemplary, reliance, or consequential damages
    of any character arising as a result of this Agreement or out of the
    use or inability to use the Work (including but not limited to
    damages for loss of goodwill, profits, data or data use, work
    stoppage, computer failure or malfunction, cost of procurement of
    substitute goods, technology or services, or any and all other
    commercial damages or losses), even if such Licensor or Contributor
    has been advised of the possibility of such damages. THESE
    LIMITATIONS SHALL APPLY NOTWITHSTANDING THE FAILURE OF THE ESSENTIAL
    PURPOSE OF ANY LIMITED REMEDY.

13. **<span class="smallcaps">Accepting Warranty or Additional
    Liability.</span>** While redistributing Works or Derivative Works
    thereof, and without limiting your obligations under Section 6, You
    may choose to offer, and charge a fee for, acceptance of support,
    warranty, indemnity, or other liability obligations and/or rights
    consistent with this License. However, in accepting such
    obligations, You may act only on Your own behalf and on Your sole
    responsibility, not on behalf of any other Contributor, and only if
    You agree to indemnify, defend, and hold Redpanda Data and each other
    Contributor harmless for any liability incurred by, or claims
    asserted against, such Contributor by reason of your accepting any
    such warranty or additional liability.

14. **<span class="smallcaps">General.</span>**

    1.  **Relationship of Parties.** You and Redpanda Data are independent
        contractors, and nothing herein shall be deemed to constitute
        either party as the agent or representative of the other or both
        parties as joint venturers or partners for any purpose.

    2.  **Export Control.** You shall comply with the U.S. Foreign
        Corrupt Practices Act and all applicable export laws,
        restrictions and regulations of the U.S. Department of Commerce,
        and any other applicable U.S. and foreign authority.

    3.  **Assignment.** This Agreement and the rights and obligations
        herein may not be assigned or transferred, in whole or in part,
        by You without the prior written consent of Redpanda Data. Any
        assignment in violation of this provision is void. This
        Agreement shall be binding upon, and inure to the benefit of,
        the successors and permitted assigns of the parties.

    4.  **Governing Law.** This Agreement shall be governed by and
        construed under the laws of the State of California and the
        United States without regard to conflicts of laws provisions
        thereof, and without regard to the Uniform Computer Information
        Transactions Act.

    5.  **Attorneys’ Fees.** In any action or proceeding to enforce
        rights under this Agreement, the prevailing party shall be
        entitled to recover its costs, expenses and attorneys’ fees.

    6.  **Severability.** If any provision of this Agreement is held to
        be invalid, illegal or unenforceable in any respect, that
        provision shall be limited or eliminated to the minimum extent
        necessary so that this Agreement otherwise remains in full force
        and effect and enforceable.

    7.  **Entire Agreement; Waivers; Modification.** This Agreement
        constitutes the entire agreement between the parties relating to
        the subject matter hereof and supersedes all proposals,
        understandings, or discussions, whether written or oral,
        relating to the subject matter of this Agreement and all past
        dealing or industry custom. The failure of either party to
        enforce its rights under this Agreement at any time for any
        period shall not be construed as a waiver of such rights. No
        changes, modifications or waivers to this Agreement will be
        effective unless in writing and signed by both parties.

```

## legal/public/bundle/free/LICENSE

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

## legal/public/license/license.go

```text
// Copyright 2024 Redpanda Data, Inc.
//
// Licensed as a Redpanda Enterprise file under the Redpanda Community
// License (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
//
// https://github.com/redpanda-data/connect/blob/main/licenses/rcl.md

package license

import (
	"github.com/redpanda-data/benthos/v4/public/service"

	"github.com/redpanda-data/connect/v4/internal/license"
)

// LocateLicenseOptBuilder represents options specified for a license locator.
type LocateLicenseOptBuilder struct {
	c license.Config
}

// LocateLicenseOptFunc defines an option to pass through the LocateLicense
// function call in order to customize its behavior.
type LocateLicenseOptFunc func(*LocateLicenseOptBuilder)

// LocateLicense attempts to locate a Redpanda Enteprise license from the
// environment and, if successful, enriches the provided resources with
// information of this license that enterprise components may reference.
func LocateLicense(res *service.Resources, opts ...LocateLicenseOptFunc) {
	optBuilder := LocateLicenseOptBuilder{}
	for _, o := range opts {
		o(&optBuilder)
	}
	license.RegisterService(res, optBuilder.c)
}

// StoreCustomLicenseBytes attempts to parse a Redpanda Enterprise license
// from a slice of bytes and, if successful, stores it within the provided
// resources pointer for enterprise components to reference.
func StoreCustomLicenseBytes(res *service.Resources, licenseBytes []byte) error {
	return license.InjectCustomLicenseBytes(res, license.Config{}, licenseBytes)
}

```

## legal/public/plugin/python/LICENSE

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
