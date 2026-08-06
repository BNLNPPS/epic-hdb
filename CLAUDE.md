# The ePIC experiment Hardware Database

## Inspiration
This project is a Django-based implementation of the Component Database,
based on the ideas of the legacy application written in Java, as described
in the legacy user guide in the file assets/docs/The_Legacy_Component_Database_User_Guide.pdf

## seed_hdb

The seed_hdb command should contain the following entities:

* Groups:
  * BEMC
  * BTOF
  * PFRICH

* Users:
  * admin - superuser, staff
  * maxim - superuser, staff
  * gnigmat - user, belongs to groups: BTOF
  * crafts - user, belongs to groups: BEMC

* Technical systems:
  * BEMC-CRYSTAL, group set to "BEMC"
  * BEMC-PM, group set to "BEMC"
  * BTOF-Sensor, group set to "BTOF"
  * BTOF-Readout, group set to "BTOF"

* Locations:
  * CUA, Storage Room
  * UIC, Test Lab

* Components:
 * PbWO4 Crystal (to be used in the BEMC-CRYSTAL technical system)
 * Hamamatsu S14160-3010PS (to be used in the BEMC-PM)
 * AC-LGAD Sensor (to be used in BTOF-Sensor technical system)
 * FCFDv2 Readout (to be used in the BTOF-Readout technical system)

 Create between 2 and 5 component instances for each component.
 