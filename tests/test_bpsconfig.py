# This file is part of ctrl_bps.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This software is dual licensed under the GNU General Public License and also
# under a 3-clause BSD license. Recipients may choose which of these licenses
# to use; please see the files gpl-3.0.txt and/or bsd_license.txt,
# respectively.  If you choose the GPL option then the following text applies
# (but note that there is still no warranty even if you opt for BSD instead):
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import os
import unittest

import yaml
from lsst.ctrl.bps import BPS_SEARCH_ORDER, BpsConfig
from lsst.daf.butler import Config

TESTDIR = os.path.abspath(os.path.dirname(__file__))


class TestBpsConfigConstructor(unittest.TestCase):
    """Test BpsConfig construction."""

    def setUp(self):
        self.filename = os.path.join(TESTDIR, "data/config.yaml")
        with open(self.filename) as f:
            self.dictionary = yaml.safe_load(f)

    def tearDown(self):
        pass

    def testFromFilenameWithoutDefaults(self):
        """Test initialization from a file, but without defaults."""
        config = BpsConfig(self.filename, defaults={})
        self.assertEqual(set(config), {"foo", "bar", "baz"} | set(BPS_SEARCH_ORDER))

    def testFromFilenameWithDefaults(self):
        """Test initialization from a file, but without defaults."""
        config = BpsConfig(self.filename, defaults={"quux": "1"})
        self.assertEqual(set(config), {"foo", "bar", "baz", "quux"} | set(BPS_SEARCH_ORDER))

    def testFromFilenameWmsFromCmdline(self):
        """Test initialization from a file with WMS class at the cmdline."""
        config = BpsConfig(
            self.filename, defaults={}, wms_service_class="wms_test_utils.WmsServiceFromCmdline"
        )
        self.assertEqual(set(config), {"foo", "bar", "baz", "corge"} | set(BPS_SEARCH_ORDER))
        self.assertEqual(config["corge"], "cmdline")

    def testFromFilenameWmsFromConfig(self):
        """Test initialization from a file with WMS class in the config."""
        filename = os.path.join(TESTDIR, "data/config_with_wms.yaml")
        config = BpsConfig(filename, defaults={})
        self.assertEqual(set(config), {"corge", "wmsServiceClass"} | set(BPS_SEARCH_ORDER))
        self.assertEqual(config["corge"], "config")

    def testFromFilenameWmsFromEnv(self):
        """Test initialization from a file with WMS class in the env."""
        os.environ["BPS_WMS_SERVICE_CLASS"] = "wms_test_utils.WmsServiceFromEnv"
        config = BpsConfig(self.filename, defaults={})
        self.assertEqual(set(config), {"foo", "bar", "baz", "corge"} | set(BPS_SEARCH_ORDER))
        self.assertEqual(config["corge"], "env")
        del os.environ["BPS_WMS_SERVICE_CLASS"]

    def testFromFilenameWmsFromDefaults(self):
        """Test initialization from a file with default WMS class."""
        config = BpsConfig(
            self.filename, defaults={"wmsServiceClass": "wms_test_utils.WmsServiceFromDefaults"}
        )
        self.assertEqual(
            set(config), {"foo", "bar", "baz", "corge", "wmsServiceClass"} | set(BPS_SEARCH_ORDER)
        )
        self.assertEqual(config["corge"], "defaults")

    def testFromDict(self):
        """Test initialization from a dictionary."""
        config = BpsConfig(self.dictionary)
        self.assertIn("bar", config)

    def testFromConfig(self):
        """Test initialization from other Config object."""
        c = Config(self.dictionary)
        config = BpsConfig(c)
        self.assertIn("baz", config)

    def testFromBpsConfig(self):
        """Test initialization from other BpsConfig object."""
        c = BpsConfig(self.dictionary)
        config = BpsConfig(c)
        self.assertIn("foo", config)

    def testInvalidArg(self):
        """Test if exception is raised for an argument of unsupported type."""
        sequence = ["wibble", "wobble", "wubble", "flob"]
        with self.assertRaises(ValueError):
            BpsConfig(sequence)


class TestBpsConfigGet(unittest.TestCase):
    """Test retrieval of items from BpsConfig."""

    def setUp(self):
        self.config = BpsConfig({"foo": "bar"})

    def tearDown(self):
        pass

    def testKeyExistsNoDefault(self):
        """Test if the value is returned when the key is in the dictionary."""
        self.assertEqual("bar", self.config.get("foo"))

    def testKeyExistsDefaultProvided(self):
        """Test if the value is returned when the key is in the dictionary."""
        self.assertEqual("bar", self.config.get("foo", "qux"))

    def testKeyMissingNoDefault(self):
        """Test if the provided default is returned if the key is missing."""
        self.assertEqual("", self.config.get("baz"))

    def testKeyMissingDefaultProvided(self):
        """Test if the custom default is returned if the key is missing."""
        self.assertEqual("qux", self.config.get("baz", "qux"))


class TestBpsConfigSearch(unittest.TestCase):
    """Test searching of BpsConfig."""

    def setUp(self):
        filename = os.path.join(TESTDIR, "data/config.yaml")
        self.config = BpsConfig(filename, search_order=["baz", "bar", "foo"], defaults={})
        os.environ["GARPLY"] = "garply"

    def tearDown(self):
        del os.environ["GARPLY"]

    def testSectionSearchOrder(self):
        """Test if sections are searched in the prescribed order."""
        key = "qux"
        found, value = self.config.search(key)
        self.assertEqual(found, True)
        self.assertEqual(value, 2)

    def testCurrentValues(self):
        """Test if a current value overrides of the one in configuration."""
        found, value = self.config.search("qux", opt={"curvals": {"qux": -3}})
        self.assertEqual(found, True)
        self.assertEqual(value, -3)

    def testSearchobjValues(self):
        """Test if a serachobj value overrides of the one in configuration."""
        options = {"searchobj": {"qux": 4}}
        found, value = self.config.search("qux", opt=options)
        self.assertEqual(found, True)
        self.assertEqual(value, 4)

    def testSubsectionSearch(self):
        options = {"curvals": {"curr_baz": "garply"}}
        found, value = self.config.search("qux", opt=options)
        self.assertEqual(found, True)
        self.assertEqual(value, 3)

    def testDefault(self):
        """Test if a default value is properly set."""
        found, value = self.config.search("plugh", opt={"default": 4})
        self.assertEqual(found, True)
        self.assertEqual(value, 4)

    def testVariables(self):
        """Test combinations of expandEnvVars, replaceEnvVars,
        and replaceVars.
        """
        test_opt = {"expandEnvVars": False, "replaceEnvVars": False, "replaceVars": False}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "${GARPLY}/waldo/{qux:03}")

        test_opt = {"expandEnvVars": False, "replaceEnvVars": False, "replaceVars": True}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "${GARPLY}/waldo/002")

        test_opt = {"expandEnvVars": False, "replaceEnvVars": True, "replaceVars": False}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "<ENV:GARPLY>/waldo/{qux:03}")

        test_opt = {"expandEnvVars": False, "replaceEnvVars": True, "replaceVars": True}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "<ENV:GARPLY>/waldo/002")

        test_opt = {"expandEnvVars": True, "replaceEnvVars": False, "replaceVars": False}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "garply/waldo/{qux:03}")

        test_opt = {"expandEnvVars": True, "replaceEnvVars": False, "replaceVars": True}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "garply/waldo/002")

        test_opt = {"expandEnvVars": True, "replaceEnvVars": True, "replaceVars": False}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(value, "garply/waldo/{qux:03}")

        test_opt = {"expandEnvVars": True, "replaceEnvVars": True, "replaceVars": True}
        found, value = self.config.search("grault", opt=test_opt)
        self.assertEqual(found, True)
        self.assertEqual(found, True)
        self.assertEqual(value, "garply/waldo/002")

    def testRequired(self):
        """Test if exception is raised if a required setting is missing."""
        with self.assertRaises(KeyError):
            self.config.search("fred", opt={"required": True})


if __name__ == "__main__":
    unittest.main()
