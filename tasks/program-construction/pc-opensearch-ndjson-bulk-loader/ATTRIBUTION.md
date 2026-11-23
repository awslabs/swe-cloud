# Attribution

Upstream: https://github.com/miku/esbulk
Revision: f655c43fc904f6d7b14862a7caa8811952b64202

Licenses and notices for distribution only; not installed in task containers.

## Corresponding source

The reference program in this task (environment/reference/program and its copy solution/reference/program) is an unmodified build of esbulk 0.7.35, licensed GPL-3.0 (below). Its complete corresponding source is the upstream repository https://github.com/miku/esbulk at commit f655c43fc904f6d7b14862a7caa8811952b64202, with the dependencies its lock files pin (go.mod, go.sum). It was built with Go 1.25.14, after `go mod download`, from the source root:

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOFLAGS=-mod=mod GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go build -trimpath -buildvcs=false -ldflags "-s -w -X github.com/miku/esbulk.Version=0.7.35" \
  -o program ./cmd/esbulk
```

This command reproduces the shipped binary byte for byte (SHA-256 5bcd61a259c99e1859142baea1510a4c810bd99eae90ea5b42ed207aaf64508b).

## legal/LICENSE

```text
                    GNU GENERAL PUBLIC LICENSE
                       Version 3, 29 June 2007

 Copyright (C) 2007 Free Software Foundation, Inc. <http://fsf.org/>
 Everyone is permitted to copy and distribute verbatim copies
 of this license document, but changing it is not allowed.

                            Preamble

  The GNU General Public License is a free, copyleft license for
software and other kinds of works.

  The licenses for most software and other practical works are designed
to take away your freedom to share and change the works.  By contrast,
the GNU General Public License is intended to guarantee your freedom to
share and change all versions of a program--to make sure it remains free
software for all its users.  We, the Free Software Foundation, use the
GNU General Public License for most of our software; it applies also to
any other work released this way by its authors.  You can apply it to
your programs, too.

  When we speak of free software, we are referring to freedom, not
price.  Our General Public Licenses are designed to make sure that you
have the freedom to distribute copies of free software (and charge for
them if you wish), that you receive source code or can get it if you
want it, that you can change the software or use pieces of it in new
free programs, and that you know you can do these things.

  To protect your rights, we need to prevent others from denying you
these rights or asking you to surrender the rights.  Therefore, you have
certain responsibilities if you distribute copies of the software, or if
you modify it: responsibilities to respect the freedom of others.

  For example, if you distribute copies of such a program, whether
gratis or for a fee, you must pass on to the recipients the same
freedoms that you received.  You must make sure that they, too, receive
or can get the source code.  And you must show them these terms so they
know their rights.

  Developers that use the GNU GPL protect your rights with two steps:
(1) assert copyright on the software, and (2) offer you this License
giving you legal permission to copy, distribute and/or modify it.

  For the developers' and authors' protection, the GPL clearly explains
that there is no warranty for this free software.  For both users' and
authors' sake, the GPL requires that modified versions be marked as
changed, so that their problems will not be attributed erroneously to
authors of previous versions.

  Some devices are designed to deny users access to install or run
modified versions of the software inside them, although the manufacturer
can do so.  This is fundamentally incompatible with the aim of
protecting users' freedom to change the software.  The systematic
pattern of such abuse occurs in the area of products for individuals to
use, which is precisely where it is most unacceptable.  Therefore, we
have designed this version of the GPL to prohibit the practice for those
products.  If such problems arise substantially in other domains, we
stand ready to extend this provision to those domains in future versions
of the GPL, as needed to protect the freedom of users.

  Finally, every program is threatened constantly by software patents.
States should not allow patents to restrict development and use of
software on general-purpose computers, but in those that do, we wish to
avoid the special danger that patents applied to a free program could
make it effectively proprietary.  To prevent this, the GPL assures that
patents cannot be used to render the program non-free.

  The precise terms and conditions for copying, distribution and
modification follow.

                       TERMS AND CONDITIONS

  0. Definitions.

  "This License" refers to version 3 of the GNU General Public License.

  "Copyright" also means copyright-like laws that apply to other kinds of
works, such as semiconductor masks.

  "The Program" refers to any copyrightable work licensed under this
License.  Each licensee is addressed as "you".  "Licensees" and
"recipients" may be individuals or organizations.

  To "modify" a work means to copy from or adapt all or part of the work
in a fashion requiring copyright permission, other than the making of an
exact copy.  The resulting work is called a "modified version" of the
earlier work or a work "based on" the earlier work.

  A "covered work" means either the unmodified Program or a work based
on the Program.

  To "propagate" a work means to do anything with it that, without
permission, would make you directly or secondarily liable for
infringement under applicable copyright law, except executing it on a
computer or modifying a private copy.  Propagation includes copying,
distribution (with or without modification), making available to the
public, and in some countries other activities as well.

  To "convey" a work means any kind of propagation that enables other
parties to make or receive copies.  Mere interaction with a user through
a computer network, with no transfer of a copy, is not conveying.

  An interactive user interface displays "Appropriate Legal Notices"
to the extent that it includes a convenient and prominently visible
feature that (1) displays an appropriate copyright notice, and (2)
tells the user that there is no warranty for the work (except to the
extent that warranties are provided), that licensees may convey the
work under this License, and how to view a copy of this License.  If
the interface presents a list of user commands or options, such as a
menu, a prominent item in the list meets this criterion.

  1. Source Code.

  The "source code" for a work means the preferred form of the work
for making modifications to it.  "Object code" means any non-source
form of a work.

  A "Standard Interface" means an interface that either is an official
standard defined by a recognized standards body, or, in the case of
interfaces specified for a particular programming language, one that
is widely used among developers working in that language.

  The "System Libraries" of an executable work include anything, other
than the work as a whole, that (a) is included in the normal form of
packaging a Major Component, but which is not part of that Major
Component, and (b) serves only to enable use of the work with that
Major Component, or to implement a Standard Interface for which an
implementation is available to the public in source code form.  A
"Major Component", in this context, means a major essential component
(kernel, window system, and so on) of the specific operating system
(if any) on which the executable work runs, or a compiler used to
produce the work, or an object code interpreter used to run it.

  The "Corresponding Source" for a work in object code form means all
the source code needed to generate, install, and (for an executable
work) run the object code and to modify the work, including scripts to
control those activities.  However, it does not include the work's
System Libraries, or general-purpose tools or generally available free
programs which are used unmodified in performing those activities but
which are not part of the work.  For example, Corresponding Source
includes interface definition files associated with source files for
the work, and the source code for shared libraries and dynamically
linked subprograms that the work is specifically designed to require,
such as by intimate data communication or control flow between those
subprograms and other parts of the work.

  The Corresponding Source need not include anything that users
can regenerate automatically from other parts of the Corresponding
Source.

  The Corresponding Source for a work in source code form is that
same work.

  2. Basic Permissions.

  All rights granted under this License are granted for the term of
copyright on the Program, and are irrevocable provided the stated
conditions are met.  This License explicitly affirms your unlimited
permission to run the unmodified Program.  The output from running a
covered work is covered by this License only if the output, given its
content, constitutes a covered work.  This License acknowledges your
rights of fair use or other equivalent, as provided by copyright law.

  You may make, run and propagate covered works that you do not
convey, without conditions so long as your license otherwise remains
in force.  You may convey covered works to others for the sole purpose
of having them make modifications exclusively for you, or provide you
with facilities for running those works, provided that you comply with
the terms of this License in conveying all material for which you do
not control copyright.  Those thus making or running the covered works
for you must do so exclusively on your behalf, under your direction
and control, on terms that prohibit them from making any copies of
your copyrighted material outside their relationship with you.

  Conveying under any other circumstances is permitted solely under
the conditions stated below.  Sublicensing is not allowed; section 10
makes it unnecessary.

  3. Protecting Users' Legal Rights From Anti-Circumvention Law.

  No covered work shall be deemed part of an effective technological
measure under any applicable law fulfilling obligations under article
11 of the WIPO copyright treaty adopted on 20 December 1996, or
similar laws prohibiting or restricting circumvention of such
measures.

  When you convey a covered work, you waive any legal power to forbid
circumvention of technological measures to the extent such circumvention
is effected by exercising rights under this License with respect to
the covered work, and you disclaim any intention to limit operation or
modification of the work as a means of enforcing, against the work's
users, your or third parties' legal rights to forbid circumvention of
technological measures.

  4. Conveying Verbatim Copies.

  You may convey verbatim copies of the Program's source code as you
receive it, in any medium, provided that you conspicuously and
appropriately publish on each copy an appropriate copyright notice;
keep intact all notices stating that this License and any
non-permissive terms added in accord with section 7 apply to the code;
keep intact all notices of the absence of any warranty; and give all
recipients a copy of this License along with the Program.

  You may charge any price or no price for each copy that you convey,
and you may offer support or warranty protection for a fee.

  5. Conveying Modified Source Versions.

  You may convey a work based on the Program, or the modifications to
produce it from the Program, in the form of source code under the
terms of section 4, provided that you also meet all of these conditions:

    a) The work must carry prominent notices stating that you modified
    it, and giving a relevant date.

    b) The work must carry prominent notices stating that it is
    released under this License and any conditions added under section
    7.  This requirement modifies the requirement in section 4 to
    "keep intact all notices".

    c) You must license the entire work, as a whole, under this
    License to anyone who comes into possession of a copy.  This
    License will therefore apply, along with any applicable section 7
    additional terms, to the whole of the work, and all its parts,
    regardless of how they are packaged.  This License gives no
    permission to license the work in any other way, but it does not
    invalidate such permission if you have separately received it.

    d) If the work has interactive user interfaces, each must display
    Appropriate Legal Notices; however, if the Program has interactive
    interfaces that do not display Appropriate Legal Notices, your
    work need not make them do so.

  A compilation of a covered work with other separate and independent
works, which are not by their nature extensions of the covered work,
and which are not combined with it such as to form a larger program,
in or on a volume of a storage or distribution medium, is called an
"aggregate" if the compilation and its resulting copyright are not
used to limit the access or legal rights of the compilation's users
beyond what the individual works permit.  Inclusion of a covered work
in an aggregate does not cause this License to apply to the other
parts of the aggregate.

  6. Conveying Non-Source Forms.

  You may convey a covered work in object code form under the terms
of sections 4 and 5, provided that you also convey the
machine-readable Corresponding Source under the terms of this License,
in one of these ways:

    a) Convey the object code in, or embodied in, a physical product
    (including a physical distribution medium), accompanied by the
    Corresponding Source fixed on a durable physical medium
    customarily used for software interchange.

    b) Convey the object code in, or embodied in, a physical product
    (including a physical distribution medium), accompanied by a
    written offer, valid for at least three years and valid for as
    long as you offer spare parts or customer support for that product
    model, to give anyone who possesses the object code either (1) a
    copy of the Corresponding Source for all the software in the
    product that is covered by this License, on a durable physical
    medium customarily used for software interchange, for a price no
    more than your reasonable cost of physically performing this
    conveying of source, or (2) access to copy the
    Corresponding Source from a network server at no charge.

    c) Convey individual copies of the object code with a copy of the
    written offer to provide the Corresponding Source.  This
    alternative is allowed only occasionally and noncommercially, and
    only if you received the object code with such an offer, in accord
    with subsection 6b.

    d) Convey the object code by offering access from a designated
    place (gratis or for a charge), and offer equivalent access to the
    Corresponding Source in the same way through the same place at no
    further charge.  You need not require recipients to copy the
    Corresponding Source along with the object code.  If the place to
    copy the object code is a network server, the Corresponding Source
    may be on a different server (operated by you or a third party)
    that supports equivalent copying facilities, provided you maintain
    clear directions next to the object code saying where to find the
    Corresponding Source.  Regardless of what server hosts the
    Corresponding Source, you remain obligated to ensure that it is
    available for as long as needed to satisfy these requirements.

    e) Convey the object code using peer-to-peer transmission, provided
    you inform other peers where the object code and Corresponding
    Source of the work are being offered to the general public at no
    charge under subsection 6d.

  A separable portion of the object code, whose source code is excluded
from the Corresponding Source as a System Library, need not be
included in conveying the object code work.

  A "User Product" is either (1) a "consumer product", which means any
tangible personal property which is normally used for personal, family,
or household purposes, or (2) anything designed or sold for incorporation
into a dwelling.  In determining whether a product is a consumer product,
doubtful cases shall be resolved in favor of coverage.  For a particular
product received by a particular user, "normally used" refers to a
typical or common use of that class of product, regardless of the status
of the particular user or of the way in which the particular user
actually uses, or expects or is expected to use, the product.  A product
is a consumer product regardless of whether the product has substantial
commercial, industrial or non-consumer uses, unless such uses represent
the only significant mode of use of the product.

  "Installation Information" for a User Product means any methods,
procedures, authorization keys, or other information required to install
and execute modified versions of a covered work in that User Product from
a modified version of its Corresponding Source.  The information must
suffice to ensure that the continued functioning of the modified object
code is in no case prevented or interfered with solely because
modification has been made.

  If you convey an object code work under this section in, or with, or
specifically for use in, a User Product, and the conveying occurs as
part of a transaction in which the right of possession and use of the
User Product is transferred to the recipient in perpetuity or for a
fixed term (regardless of how the transaction is characterized), the
Corresponding Source conveyed under this section must be accompanied
by the Installation Information.  But this requirement does not apply
if neither you nor any third party retains the ability to install
modified object code on the User Product (for example, the work has
been installed in ROM).

  The requirement to provide Installation Information does not include a
requirement to continue to provide support service, warranty, or updates
for a work that has been modified or installed by the recipient, or for
the User Product in which it has been modified or installed.  Access to a
network may be denied when the modification itself materially and
adversely affects the operation of the network or violates the rules and
protocols for communication across the network.

  Corresponding Source conveyed, and Installation Information provided,
in accord with this section must be in a format that is publicly
documented (and with an implementation available to the public in
source code form), and must require no special password or key for
unpacking, reading or copying.

  7. Additional Terms.

  "Additional permissions" are terms that supplement the terms of this
License by making exceptions from one or more of its conditions.
Additional permissions that are applicable to the entire Program shall
be treated as though they were included in this License, to the extent
that they are valid under applicable law.  If additional permissions
apply only to part of the Program, that part may be used separately
under those permissions, but the entire Program remains governed by
this License without regard to the additional permissions.

  When you convey a copy of a covered work, you may at your option
remove any additional permissions from that copy, or from any part of
it.  (Additional permissions may be written to require their own
removal in certain cases when you modify the work.)  You may place
additional permissions on material, added by you to a covered work,
for which you have or can give appropriate copyright permission.

  Notwithstanding any other provision of this License, for material you
add to a covered work, you may (if authorized by the copyright holders of
that material) supplement the terms of this License with terms:

    a) Disclaiming warranty or limiting liability differently from the
    terms of sections 15 and 16 of this License; or

    b) Requiring preservation of specified reasonable legal notices or
    author attributions in that material or in the Appropriate Legal
    Notices displayed by works containing it; or

    c) Prohibiting misrepresentation of the origin of that material, or
    requiring that modified versions of such material be marked in
    reasonable ways as different from the original version; or

    d) Limiting the use for publicity purposes of names of licensors or
    authors of the material; or

    e) Declining to grant rights under trademark law for use of some
    trade names, trademarks, or service marks; or

    f) Requiring indemnification of licensors and authors of that
    material by anyone who conveys the material (or modified versions of
    it) with contractual assumptions of liability to the recipient, for
    any liability that these contractual assumptions directly impose on
    those licensors and authors.

  All other non-permissive additional terms are considered "further
restrictions" within the meaning of section 10.  If the Program as you
received it, or any part of it, contains a notice stating that it is
governed by this License along with a term that is a further
restriction, you may remove that term.  If a license document contains
a further restriction but permits relicensing or conveying under this
License, you may add to a covered work material governed by the terms
of that license document, provided that the further restriction does
not survive such relicensing or conveying.

  If you add terms to a covered work in accord with this section, you
must place, in the relevant source files, a statement of the
additional terms that apply to those files, or a notice indicating
where to find the applicable terms.

  Additional terms, permissive or non-permissive, may be stated in the
form of a separately written license, or stated as exceptions;
the above requirements apply either way.

  8. Termination.

  You may not propagate or modify a covered work except as expressly
provided under this License.  Any attempt otherwise to propagate or
modify it is void, and will automatically terminate your rights under
this License (including any patent licenses granted under the third
paragraph of section 11).

  However, if you cease all violation of this License, then your
license from a particular copyright holder is reinstated (a)
provisionally, unless and until the copyright holder explicitly and
finally terminates your license, and (b) permanently, if the copyright
holder fails to notify you of the violation by some reasonable means
prior to 60 days after the cessation.

  Moreover, your license from a particular copyright holder is
reinstated permanently if the copyright holder notifies you of the
violation by some reasonable means, this is the first time you have
received notice of violation of this License (for any work) from that
copyright holder, and you cure the violation prior to 30 days after
your receipt of the notice.

  Termination of your rights under this section does not terminate the
licenses of parties who have received copies or rights from you under
this License.  If your rights have been terminated and not permanently
reinstated, you do not qualify to receive new licenses for the same
material under section 10.

  9. Acceptance Not Required for Having Copies.

  You are not required to accept this License in order to receive or
run a copy of the Program.  Ancillary propagation of a covered work
occurring solely as a consequence of using peer-to-peer transmission
to receive a copy likewise does not require acceptance.  However,
nothing other than this License grants you permission to propagate or
modify any covered work.  These actions infringe copyright if you do
not accept this License.  Therefore, by modifying or propagating a
covered work, you indicate your acceptance of this License to do so.

  10. Automatic Licensing of Downstream Recipients.

  Each time you convey a covered work, the recipient automatically
receives a license from the original licensors, to run, modify and
propagate that work, subject to this License.  You are not responsible
for enforcing compliance by third parties with this License.

  An "entity transaction" is a transaction transferring control of an
organization, or substantially all assets of one, or subdividing an
organization, or merging organizations.  If propagation of a covered
work results from an entity transaction, each party to that
transaction who receives a copy of the work also receives whatever
licenses to the work the party's predecessor in interest had or could
give under the previous paragraph, plus a right to possession of the
Corresponding Source of the work from the predecessor in interest, if
the predecessor has it or can get it with reasonable efforts.

  You may not impose any further restrictions on the exercise of the
rights granted or affirmed under this License.  For example, you may
not impose a license fee, royalty, or other charge for exercise of
rights granted under this License, and you may not initiate litigation
(including a cross-claim or counterclaim in a lawsuit) alleging that
any patent claim is infringed by making, using, selling, offering for
sale, or importing the Program or any portion of it.

  11. Patents.

  A "contributor" is a copyright holder who authorizes use under this
License of the Program or a work on which the Program is based.  The
work thus licensed is called the contributor's "contributor version".

  A contributor's "essential patent claims" are all patent claims
owned or controlled by the contributor, whether already acquired or
hereafter acquired, that would be infringed by some manner, permitted
by this License, of making, using, or selling its contributor version,
but do not include claims that would be infringed only as a
consequence of further modification of the contributor version.  For
purposes of this definition, "control" includes the right to grant
patent sublicenses in a manner consistent with the requirements of
this License.

  Each contributor grants you a non-exclusive, worldwide, royalty-free
patent license under the contributor's essential patent claims, to
make, use, sell, offer for sale, import and otherwise run, modify and
propagate the contents of its contributor version.

  In the following three paragraphs, a "patent license" is any express
agreement or commitment, however denominated, not to enforce a patent
(such as an express permission to practice a patent or covenant not to
sue for patent infringement).  To "grant" such a patent license to a
party means to make such an agreement or commitment not to enforce a
patent against the party.

  If you convey a covered work, knowingly relying on a patent license,
and the Corresponding Source of the work is not available for anyone
to copy, free of charge and under the terms of this License, through a
publicly available network server or other readily accessible means,
then you must either (1) cause the Corresponding Source to be so
available, or (2) arrange to deprive yourself of the benefit of the
patent license for this particular work, or (3) arrange, in a manner
consistent with the requirements of this License, to extend the patent
license to downstream recipients.  "Knowingly relying" means you have
actual knowledge that, but for the patent license, your conveying the
covered work in a country, or your recipient's use of the covered work
in a country, would infringe one or more identifiable patents in that
country that you have reason to believe are valid.

  If, pursuant to or in connection with a single transaction or
arrangement, you convey, or propagate by procuring conveyance of, a
covered work, and grant a patent license to some of the parties
receiving the covered work authorizing them to use, propagate, modify
or convey a specific copy of the covered work, then the patent license
you grant is automatically extended to all recipients of the covered
work and works based on it.

  A patent license is "discriminatory" if it does not include within
the scope of its coverage, prohibits the exercise of, or is
conditioned on the non-exercise of one or more of the rights that are
specifically granted under this License.  You may not convey a covered
work if you are a party to an arrangement with a third party that is
in the business of distributing software, under which you make payment
to the third party based on the extent of your activity of conveying
the work, and under which the third party grants, to any of the
parties who would receive the covered work from you, a discriminatory
patent license (a) in connection with copies of the covered work
conveyed by you (or copies made from those copies), or (b) primarily
for and in connection with specific products or compilations that
contain the covered work, unless you entered into that arrangement,
or that patent license was granted, prior to 28 March 2007.

  Nothing in this License shall be construed as excluding or limiting
any implied license or other defenses to infringement that may
otherwise be available to you under applicable patent law.

  12. No Surrender of Others' Freedom.

  If conditions are imposed on you (whether by court order, agreement or
otherwise) that contradict the conditions of this License, they do not
excuse you from the conditions of this License.  If you cannot convey a
covered work so as to satisfy simultaneously your obligations under this
License and any other pertinent obligations, then as a consequence you may
not convey it at all.  For example, if you agree to terms that obligate you
to collect a royalty for further conveying from those to whom you convey
the Program, the only way you could satisfy both those terms and this
License would be to refrain entirely from conveying the Program.

  13. Use with the GNU Affero General Public License.

  Notwithstanding any other provision of this License, you have
permission to link or combine any covered work with a work licensed
under version 3 of the GNU Affero General Public License into a single
combined work, and to convey the resulting work.  The terms of this
License will continue to apply to the part which is the covered work,
but the special requirements of the GNU Affero General Public License,
section 13, concerning interaction through a network will apply to the
combination as such.

  14. Revised Versions of this License.

  The Free Software Foundation may publish revised and/or new versions of
the GNU General Public License from time to time.  Such new versions will
be similar in spirit to the present version, but may differ in detail to
address new problems or concerns.

  Each version is given a distinguishing version number.  If the
Program specifies that a certain numbered version of the GNU General
Public License "or any later version" applies to it, you have the
option of following the terms and conditions either of that numbered
version or of any later version published by the Free Software
Foundation.  If the Program does not specify a version number of the
GNU General Public License, you may choose any version ever published
by the Free Software Foundation.

  If the Program specifies that a proxy can decide which future
versions of the GNU General Public License can be used, that proxy's
public statement of acceptance of a version permanently authorizes you
to choose that version for the Program.

  Later license versions may give you additional or different
permissions.  However, no additional obligations are imposed on any
author or copyright holder as a result of your choosing to follow a
later version.

  15. Disclaimer of Warranty.

  THERE IS NO WARRANTY FOR THE PROGRAM, TO THE EXTENT PERMITTED BY
APPLICABLE LAW.  EXCEPT WHEN OTHERWISE STATED IN WRITING THE COPYRIGHT
HOLDERS AND/OR OTHER PARTIES PROVIDE THE PROGRAM "AS IS" WITHOUT WARRANTY
OF ANY KIND, EITHER EXPRESSED OR IMPLIED, INCLUDING, BUT NOT LIMITED TO,
THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
PURPOSE.  THE ENTIRE RISK AS TO THE QUALITY AND PERFORMANCE OF THE PROGRAM
IS WITH YOU.  SHOULD THE PROGRAM PROVE DEFECTIVE, YOU ASSUME THE COST OF
ALL NECESSARY SERVICING, REPAIR OR CORRECTION.

  16. Limitation of Liability.

  IN NO EVENT UNLESS REQUIRED BY APPLICABLE LAW OR AGREED TO IN WRITING
WILL ANY COPYRIGHT HOLDER, OR ANY OTHER PARTY WHO MODIFIES AND/OR CONVEYS
THE PROGRAM AS PERMITTED ABOVE, BE LIABLE TO YOU FOR DAMAGES, INCLUDING ANY
GENERAL, SPECIAL, INCIDENTAL OR CONSEQUENTIAL DAMAGES ARISING OUT OF THE
USE OR INABILITY TO USE THE PROGRAM (INCLUDING BUT NOT LIMITED TO LOSS OF
DATA OR DATA BEING RENDERED INACCURATE OR LOSSES SUSTAINED BY YOU OR THIRD
PARTIES OR A FAILURE OF THE PROGRAM TO OPERATE WITH ANY OTHER PROGRAMS),
EVEN IF SUCH HOLDER OR OTHER PARTY HAS BEEN ADVISED OF THE POSSIBILITY OF
SUCH DAMAGES.

  17. Interpretation of Sections 15 and 16.

  If the disclaimer of warranty and limitation of liability provided
above cannot be given local legal effect according to their terms,
reviewing courts shall apply local law that most closely approximates
an absolute waiver of all civil liability in connection with the
Program, unless a warranty or assumption of liability accompanies a
copy of the Program in return for a fee.

                     END OF TERMS AND CONDITIONS

            How to Apply These Terms to Your New Programs

  If you develop a new program, and you want it to be of the greatest
possible use to the public, the best way to achieve this is to make it
free software which everyone can redistribute and change under these terms.

  To do so, attach the following notices to the program.  It is safest
to attach them to the start of each source file to most effectively
state the exclusion of warranty; and each file should have at least
the "copyright" line and a pointer to where the full notice is found.

    <one line to give the program's name and a brief idea of what it does.>
    Copyright (C) <year>  <name of author>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

Also add information on how to contact you by electronic and paper mail.

  If the program does terminal interaction, make it output a short
notice like this when it starts in an interactive mode:

    <program>  Copyright (C) <year>  <name of author>
    This program comes with ABSOLUTELY NO WARRANTY; for details type `show w'.
    This is free software, and you are welcome to redistribute it
    under certain conditions; type `show c' for details.

The hypothetical commands `show w' and `show c' should show the appropriate
parts of the General Public License.  Of course, your program's commands
might be different; for a GUI interface, you would use an "about box".

  You should also get your employer (if you work as a programmer) or school,
if any, to sign a "copyright disclaimer" for the program, if necessary.
For more information on this, and how to apply and follow the GNU GPL, see
<http://www.gnu.org/licenses/>.

  The GNU General Public License does not permit incorporating your program
into proprietary programs.  If your program is a subroutine library, you
may consider it more useful to permit linking proprietary applications with
the library.  If this is what you want to do, use the GNU Lesser General
Public License instead of this License.  But first, please read
<http://www.gnu.org/philosophy/why-not-lgpl.html>.

```

## legal/docs/repo/authors.json

```text
{"y": [[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 26, 25, 24, 24, 24, 24, 22, 21, 19], [310, 373, 379, 457, 457, 461, 461, 490, 491, 493, 563, 640, 642, 752, 752, 766, 886, 881, 905, 932, 927, 928, 950, 953, 1199, 1206, 1236, 1331, 1259], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 113], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 3, 3, 3, 3, 3, 3, 3, 3], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 63, 60, 60, 60, 60, 60, 60, 60, 58, 57]], "ts": ["2014-08-29T09:15:42", "2014-09-29T16:29:54", "2014-10-23T10:10:41", "2014-12-01T14:47:43", "2014-12-18T11:50:14", "2015-01-19T14:14:20", "2015-04-03T12:02:57", "2015-05-07T18:44:13", "2015-07-16T12:19:35", "2015-09-16T02:42:00", "2015-11-10T12:10:00", "2016-06-22T17:55:53", "2016-07-22T20:56:16", "2016-09-09T12:05:52", "2016-10-04T10:44:59", "2016-11-26T12:29:48", "2016-12-04T21:02:40", "2016-12-12T02:09:42", "2017-01-02T15:42:27", "2017-06-29T11:28:18", "2017-08-18T15:27:59", "2017-09-25T22:06:41", "2017-10-18T10:21:14", "2018-01-16T20:37:57", "2018-01-27T22:29:02", "2018-02-11T14:49:01", "2018-04-10T20:54:50", "2018-04-19T08:47:42", "2018-05-02T10:17:07"], "labels": ["Klaubert Herr", "Martin Czygan", "Yusuke KUOKA", "klaubert", "sakshambathla"]}
```

## legal/docs/repo/authors.png

```text
‰PNG

   IHDR        Rİîi   sBIT|dˆ   	pHYs  a  a¨?§i   9tEXtSoftware matplotlib version 2.2.2, http://matplotlib.org/†ŸÔ    IDATxœìİy\UÕşÿñ÷a’¤˜RDÇÌærÆ!KÍ,§œ²¼efêMë–bå­LÍ¡2çKjZY×Ê2Íñ—æ„&*9`Z¤)cÈt~øåÜN ”sàëùxğx¸÷Z{­Ï>>x·ö^£Ñh       XÀÁŞ       ¨:      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XŒ@      €Å      XÌÉŞ ’TRR¢ß~ûM^^^2ö.     üÑhTvv¶‚ƒƒåàÀµ"®¿ıö›BBBì]     ¸‚S§Néæ›o¶w°#E\¼¼¼$]üKÉÛÛÛÎÕ     €¿ËÊÊRHHˆéwxÜ¸q](}ÌÙÛÛ›@    €ë¯*¼      °"       ‹(      °ïPD•Q\\¬ÂÂB{—jÌÙÙYö.    €ë"®{F£Qééé:ş¼½KÁÀ××WAAA¼d    €K PÄu¯4L»»;A¬Âh4*//O’¤:uêØ¹"     ®OŠ¸®›ÂD???{—ƒjÎÍÍM’”‘‘¡€€      lÊ‚ëZé;İİİí\	n¥ßk¼¯    €ò(¢Jà1gØ
ßk     \"       ‹(vÔªU+9²RÇŒWÓ¦M+uL     €RlÊ‚*+í©§l:_9s*ÔàÀ:ş¼V¯^m:·jÕ*õë×O“&MÒèÑ£+»D«[¼x±F©óçÏ_u?ƒÁ O?ıTİºu³V™     ÀŠX¡ØÈüùóÕ·o_Í=»J†‰×Ë&%:     *"`“'OÖ³Ï>«åË—kĞ A—ì— -ZÈËËKAAAêÓ§222Lí‹/–¯¯¯Ù5«W¯.w#‘9sæ($$DîîîêÕ«—233ÍÚçÏŸ¯èèh¹ººªAƒzÿı÷Mm'NœÁ`ĞŠ+tÿı÷ËÕÕU‰‰‰4h233e0d0•ŸÈÿœ:uJ½zõ’¯¯¯jÕª¥‡zH'Nœ0µ8PİºuÓ¤I“¬¨¨(IRxx¸^{í5=şøãòööÖ“O>yÍµ     €+#P¬lìØ±zíµ×´fÍuïŞı²}õÚk¯iß¾}Z½zµNœ8¡VxÎ£Gê£>Òÿû_­]»V{÷îÕÓO?mjOLLÔøñã5iÒ$:tHÿş÷¿õÊ+¯hÉ’%fãŒ7NÏ=÷œ:¤Ö­[kúôéòööVZZšÒÒÒ4fÌ˜
×ö÷û‹‹“———¶lÙ¢mÛ¶ÉÓÓS;v4[q¸~ız¥¤¤hİºuZ³féü”)SÔ¤Iíİ»W¯¼òÊ5Õ     ,Ã;+úê«¯ôÙgŸiıúõjÓ¦Íû<Øôç[n¹E3gÎÔm·İ¦œœyzzZ<o~~¾–.]ª›nºI’4kÖ,uéÒES§NUPP&L˜ ©S§ªG’¤ˆˆ<xPsæÌÑ€LãŒ9ÒÔG’|||d0tÅ233¯XóŠ+TRR¢ùóç›VY.Z´H¾¾¾Ú¸q£:tè IòğğĞüùóåââbv}›6mªäãã     TeŠ€5nÜXgÎœÑ„	Ô²eË+l»wïV||¼öíÛ§sçÎ©¤¤D’tòäI5lØĞâyCCCMa¢$İyç*))QJJŠ¼¼¼tìØ12DC‡5õ)**’Ù8-Z´°xÎ¿óòòÒ={ÊœŒŒ4ıyß¾}:zô¨¼¼¼ÌúäççëØ±c¦ãF•	¯µ>     pu+ºé¦›´jÕ*µnİZ;vÔW_}U&<+•››«¸¸8ÅÅÅ)11Qşşş:yò¤âââLÿ:88Èh4š]WÑÍRrrr$IóæÍÓí·ßnÖæèèhvìááQ¡±ÿÊÁÁAõêÕ»b-·Şz«Ë´ùûû_±k©     \EÀÊÂÂÂ´iÓ&S¨¸víÚrCÅÃ‡ëìÙ³zóÍ7"IÚµk—Yegg+77×¦%%%•ëäÉ“úí·ß,IÚ¾}»¥ÀÀ@ëøñãêÛ·o…îÅÅÅEÅÅÅºærš7o®+V(  @ŞŞŞ•6.     °6el $$D7nTFF†âââ”••U¦Ohh¨\\\4kÖ,?~\Ÿş¹^{í5³>·ß~»ÜİİõÒK/éØ±cúğÃµxñâ2c¹ººjÀ€Ú·oŸ¶lÙ¢#F¨W¯^¦wNœ8Qo¼ñ†fÎœ©Ÿ~úI?şø£-Z¤iÓ¦]ö>ÂÃÃ•““£õë×ëÌ™3ÊËË»úERß¾}U»vm=ôĞCÚ²e‹RSSµqãF1B¿üòË5     ¬ƒ@°‘›o¾Y7nÔ™3gÊıııµxñb­\¹R6Ô›o¾©)S¦˜õ©U«–şóŸÿèË/¿T£F´lÙ2ÅÇÇ—™«^½zêÑ£‡:wî¬:¨qãÆzÿı÷MíO<ñ„æÏŸ¯E‹©Q£FºÿşûµxñbEDD\öîºë.6L>ú¨üıı5yòä«ÿ@$¹»»kóæÍ
U=­!C†(??Ÿ‹     \§Æ¿¿°ƒ¬¬,ùøø(33Ó,HÊÏÏWjjª"""äêêjÇ
q£à{    Êw©ßİqãa…"       ‹(      °"       ‹(      °"     €–(3³ĞŞe W@     À†Ö­;­sç
ì]pÕ     läüù­]›nï2€kB      `#Ÿ|ò«.\(±wÀ5!Pª©ğğpMŸ>İŞe     şÏñã9úá‡?ì]pÍ+8p ƒ†V¦í™g‘Á`ĞÀ¯yÅ‹Ë××·Ìù;wêÉ'Ÿ¼æñ=ªAƒéæ›oV5¡Ş½{k×®]×<6    Ü(ŒF£V¬8%£ÑŞ• ×ÎÉŞ Wë)Ï7ç*®			ÑòåËõÎ;ïÈÍÍM’”ŸŸ¯?üP¡¡¡×\Saá¥wó÷÷¿æñwíÚ¥¶mÛ*66VsæÌQƒ”­Ï>ûL£GÖ¦M›®y    ¸|ÿıY8‘gï2€JÁ
EÀŠš7o®}òÉ'¦sŸ|ò‰BCCÕ¬Y3³¾k×®Õ=÷Ü#___ùùùéĞ±cÇLí'NœÁ`ĞŠ+tÿı÷ËÕÕU‰‰‰4h233e0d0/©ì#ÏƒAóçÏW÷îİåîî®ÈÈH}şùç—¬İh4jàÀŠŒŒÔ–-[Ô¥KÕ­[WM›6Õ„	ôÙgŸI’âããMsÿõkñâÅZºt©üüütáÂ³±»uë¦şıû›_ıuÈËËKO<ñ„Æ§¦M›šÚwîÜ©öíÛ«víÚòññÑı÷ß¯={ö˜YÑû    [ÉÏ/Ö§Ÿşjï2€JC XÙàÁƒµhÑ"ÓñÂ…5hĞ 2ırss5jÔ(íÚµKë×¯—ƒƒƒºwï®’ó—õ7NÏ=÷œ:¤Ö­[kúôéòööVZZšÒÒÒ4fÌ˜KÖ2qâDõêÕKû÷ïWçÎÕ·o_ıñGùïïHJJRrr²F-‡²U”>f=fÌÓÜiiiš2eŠÜİİÕ¢E=òÈ#*..6ö222ôÅ_hğàÁ’¤ÄÄDMš4Io½õ–vïŞ­ĞĞPÍ=Ûl®ììl0@[·nÕöíÛ©Î;+;;ûªï    lå‹/Ò”•Udï2€JC XY¿~ı´uëVıüóÏúùçŸµmÛ6õë×¯L¿‡~X=zôP½zõÔ´iS-\¸P?şø£<hÖoäÈ‘êÑ£‡"""&)((H—¬eàÀêİ»·êÕ«§ÿûßÊÉÉÑ?üPnß#GH’4hpÙûóôô4Í}âÄ	½üòËZ´h‘bccåææ¦>}ú˜ªÿùÏªV­ZI’fÍš¥!C†hĞ Aª_¿¾Æ¯F™ÍÑ¦Mõë×O4Ptt´æÎ«¼¼¼2\Wäş    À22òµaC†½Ë *"`eşşşêÒ¥‹/^¬E‹©K—.ª]»v™~GQïŞ½uË-·ÈÛÛ[ááá’¤“'OšõkÑ¢ÅU×Ò¸qcÓŸ=<<äíí­ŒŒòÿa3VğMÁ'OT·nİ4fÌõêÕËt~èĞ¡úæ›oôë¯—÷/^¼Ø´a$¥¤¤¨eË–fcııøôéÓ:t¨"##åãã#oooåää”ùl*r    `+Wş¢¢"vbAõÂ¦,€<XÃ‡—$½÷Ş{åöéÚµ«ÂÂÂ4oŞ<«¤¤D±±±*((0ëçááqÕu8;;›†2T—ª_¿¾$éğáÃeŞ÷øw¹¹¹zğÁuçwêÕW_5kkÖ¬™š4i¢¥K—ªC‡JNNÖ_|Q¡º ³gÏjÆŒ
S5tçw–ùl*r    `mÉÉ™Ú¿?ÓŞe •Š€tìØQ*,,T\\\™ö³gÏ*%%E/¿ü²Ú¶m«èèh;wÎ¢±]\\T\\\Ù%«iÓ¦jØ°¡¦NZn(wşüyIW2öë×O%%%JHH0­<ü«'xÂ´B³]»v
		1µEEEiçÎfıÿ~¼mÛ61B;wVLLŒjÔ¨¡3gÎTÆm   €Uµrå/ö.°
EÀuèĞ!<xPeÚkÖ¬)???Í;WGÕ†4jÔ(‹ÆWNNÖ¯_¯3gÎ(//¯Rj6Z´h‘~úé'İ{ï½úòË/uüøqíß¿_“&MÒC=$éâ.Ïß~û­æÌ™£œœ¥§§+==]şù§i¬>}úè—_~Ñ¼yóL›±”zöÙgµ`Á-Y²DGÑë¯¿®ıû÷›“‘‘‘JHHĞ¡C‡´cÇõíÛWnnn•rŸ    `ß}—¡´´|{—X"`#ŞŞŞòöö.·ÍÁÁAË—/×îİ»«çŸ^o¿ı¶EãŞu×]6l˜}ôQùûûkòäÉ•VsË–-µk×.Õ«WOC‡Utt´|ğA%''kúôé’¤M›6)''Gwİu—êÔ©cúZ±b…i=üğÃòôôT·nİÌæèÛ·¯^|ñE3FÍ›7Wjjª(WWWSŸèÜ¹sjŞ¼¹ú÷ï¯#F(   Òî    *SNN‘¾ø"ÍŞe Vc0VtçÀ
²²²äãã£ÌÌL³Ğ-??_©©©Šˆˆ0˜Põ´mÛV111š9sæû¶oß^AAAJHH°Aeæø   p­–-;©¿lŸ_l ğğ«G¾=\êwwÜxX¡XmŞ¼Y]»vUpp°ƒV¯^}É¾Ã†“Á`0­:+õÇ¨oß¾òöö–¯¯¯†¢œœ³>û÷ï×½÷Ş+WWW…„„Tê
9T}çÎÓ§Ÿ~ª7ê™g)Ó——§iÓ¦)99Y‡Ö„	ôí·ßjÀ€v¨    ®]JJ¶½K ¬Š@±ËÍÍU“&M.¹«p©O?ıTÛ·oWppp™¶¾}û*99YëÖ­Óš5k´yóf=ùä“¦ö¬¬,uèĞAaaaÚ½{·Ş~ûmÅÇÇkîÜ¹•~?¨šš5k¦ê­·ŞRTTT™vƒÁ /¿üR÷İwŸn½õVı÷¿ÿÕÇ¬víÚÙ¡Z    ¸6ÅÅFışû{—X•“½€õtêÔI:uºlŸ_ıUÏ>û¬¾şúkuéÒÅ¬íĞ¡CZ»v­vîÜ©-ZH’fÍš¥Î;kÊ”)
Vbb¢


´páB¹¸¸(&&FIIIš6mšYğˆ×‰'.Ûîææ¦o¿ıÖ6Å    €•9sAEE¼]Õ+o`%%%êß¿¿şùÏ*&&¦Lû÷ß/___S˜(IíÚµ“ƒƒƒvìØaêsß}÷ÉÅÅÅÔ'..N))):wîœõo    €ëHz:;;£úc…âì­·Ş’“““FŒQn{zzz™tœœT«V-¥§§›úDDD˜õ	4µÕ¬Y³Ü±/\¸ ş·<++ëªï    €ë"n¬P¼AíŞ½[3fÌĞâÅ‹e0l>ÿo¼!ÓWHHˆÍk      ²(âF@ xƒÚ²e‹222*'''999éçŸÖèÑ£.I


RFF†ÙuEEEúã?dêsúôi³>¥Ç¥}Êóâ‹/*33ÓôuêÔ©J¼;     ìƒ@7y¾Aõïß¿Ì.ºqqqêß¿¿$IºóÎ;uşüyíŞ½[·Şz«$iÃ†*))Ñí·ßnêó¯ıK………rvv–$­[·NQQQ—|ÜY’jÔ¨¡5jXãÖ     °›Ó§¯(::äê“/ÉÃúV@ XåääèèÑ£¦ãÔÔT%%%©V­Z
•ŸŸŸYggg)**J’­;jèĞ¡úàƒTXX¨áÃ‡ë±ÇSpp°$©OŸ>š8q¢†¢±cÇêÀš1c†ŞyçÛİ(     ×¬¬Båæ_±_ÓûóåéUhƒŠ ëà‘çjl×®]jÖ¬™š5k&I5j”š5k¦ñãÇ[<Fbb¢4h ¶mÛªsçÎºç{4wî\S»¾ùæ¥¦¦êÖ[oÕèÑ£5~üx=ùä“•~?¨¸{î¹GcÆŒ±w    pC°du¢»»ƒš´9.ÏœK¿&¸Ş(Vc­Zµ’Ñh,óµxñârûŸ8qB#G4;W«V-}øá‡ÊÎÎVff¦.\(OOO³>7Ö–-[”ŸŸ¯_~ùEcÇµÖ-UF£QíÚµS\\\™¶÷ß_¾¾¾úå—_ìPÙµë×¯ŸzöìivnÅŠruuÕŒ3$]:Èœ?¾j×®mvîÌ™31b„ÂÂÂäââ¢àà`2¤ÌçcÉ¼¥¶lÙ"GGG=ôĞCW}Ÿ    PQ–¼?±E—ßs¬½ª¬‡GQu­±ñ|XŞÕ`0hÑ¢EjÔ¨‘æÌ™£§zJÒÅÇÎ_xáÍ=[7ß|³•
µ­>ø@Ï=÷œæÍ›§Ç¼B×9sFwÜq‡Üİİ5gÎ5lØPÇ×¿şõ/µhÑBÛ·o7mT‘y,X`j;}ú´¯öö    ÀbW
ıÔ¤Y¶<¿çwTm¬P¬$$$D3fÌĞ˜1c”šš*£Ñ¨!C†¨C‡êß¿¿^~ùeµhÑÂìš)S¦¨^½z¦ãõë×ë¶Ûn“»»»jÖ¬©{î¹Ç´r¯¼{Ã‡/³ÙÎ_}şùçòööÖòåËMçæÌ™£ÈÕÕUÑÑÑš3gÅ÷øïÿ[Ï?ÿ¼V®\Yá0Qº¸Û÷éÓ§õí·ßªcÇ
U«V­´nİ:988èÙgŸ­ğ¼YYYZµj•yæÅÅÅiÉ’%®    ®Æ•Åİ)*©“ª¬‡Š€0@Ÿ~ú©¬=zèÀJNN¶èÚÂÂBuïŞ]O?ı´V¬X¡.hÇW]KBB‚yæ­X±B:]ülÉ’%zíµ×4kÖ,5mÚT{öìÑO<!OOOõíÛ÷’cF3FsçÎÕW_}¥V­ZU¸ââb}ôÑGzüñÇ`Öæîî®aÃ†iâÄ‰ÊÌÌ”Åó._¾\5RİºuÕ¯_?7N/¼ğB…ë   €Šº\ xK”ƒšúÊùWwVX"`esçÎULLŒ6oŞ¬?şXşşş]wîÜ9egg«k×®ºå–[$]ÜyûjÌ˜1Cñññúâ‹/tï½÷šÎO˜0Aï¼óºwï.IŠˆˆĞ?ş¨9sæ\6P\³f


´iÓ&İwß}WUSzzº²²².yOÑÑÑ*))Ñ±cÇÔ¼ys‹ç]°`iÕbçÎ5dÈmİºU÷ÜsÏUÕ	    –(,,ÑÙ³å¶RóîÉ
Kêmãª ëà‘gÀÊôÔSO)::Zİºu«ĞuıúõS»víôàƒjæÌ™JOO¯ğü+V¬Ğ/¼ õë×›…‰™™™úùçŸ5`À yzzš¾Ş|óM;vì²c6mÚT¡¡¡zå•W”››[ášşÊh4ZÜ÷Jó&''kïŞ½zôÑG%I...zä‘G´`Á‚kª    ®$#ã‚.õëMã»ŠÔ¸ ‰Jm[`%Š€899ÉÉÉ|A°ƒƒC™0­°°Ğì8!!AÛ¶mÓwÜ¡eË–©~ıúÚ¹s§Å×KRóæÍåçç§…šÏÉÉ‘$-Z´HIII¦¯hëÖ­—½ŸmÚ´I?ÿü³:uêd«”···233Ë\wşüyÓãËAAAòòòÒ¡C‡ÊãĞ¡CrppPİºu-wÁ‚*,,TPPé3Ÿ7oV®\©ìììËŞ    \‹K=î\£†ƒštüIGÙ¸"Àz;ñ÷÷/³â0))©L¿æÍ›ë¥—^Ò÷ß¯¨¨(-[¶Ìt}ZZÚ¯ŒŒÔ†´jÕ*9Òt>88X:~ü¸êÕ«göqÅúÃÃÃµiÓ&ıòË/eÂ½¨¨(íŞ½»Ì5{öìQıúõ%Izä‘G” ŒŒ³~yyyš={¶:wîl
 ¯4oAA4}út³€tß¾}ª]»¶V¬XqÅ{   €«u©@±EÜ95:ÙÆÆÕ ÖE ØIëÖ­•––¦)S¦èèÑ£š5k–Ö­[gj?zô¨)H<yò¤Ö®]«cÇ™Ş9Ø¦Mmß¾]‰‰‰:räˆ^~ùe>|¸Ü¹4h 6hÙ²e3fŒ$É`0(>>^¯¿şºŞ}÷]ıôÓOÚ¿¿,X éÓ§[taaaÚ¸q£~ûí7uìØÑî=óÌ3:xğ şyıøã:|ø°¦L™¢•+WjôèÑ¦ëß|óMùûû«}ûöúúë¯uêÔ)mÚ´I:tÑhÔ¬Y³,÷óÏ?WNN†¢ØØX³¯‡~˜Ç   XUybÍZjrÇy§ßd‡Š ë!Pì$66V³fÍÒŒ3L;,?ÿüó¦v<xP=zôPdd¤şñhäÈ‘zâ‰'$I]ºtÑK/½¤Q£F©eË–ºpáÂe7RiØ°¡6lØ ¥K—jìØ±’¤aÃ†éƒ>ĞüùóÕ¨Q#µnİZ			­P,ª7*==]qqqÊÎÎV½zõ´iÓ&8p@mÚ´ÑwÜ¡?şXü±ÚµkgºÖßß_;vìĞ½÷Ş«'xBuëÖÕcLŞZ    IDAT=fz´;<<Üâyß{ï=uèĞAeú>üğÃÚ¾}»<hñ}   @E”(¶xè¤üØÑÕ Öe0VdGÀJ²²²äãã£ÌÌLy{{›Îççç+55Uruuµc…¸Qğ=   àjŒ±W.”˜C#õèàtÕûáâ=’|mS[e¹Ôïî¸ñ°B     àœ;W`&JRó‡*bO[;UX"     À5øûãÎ1·ÕÄ±¾‹œíT`]Š      ×àïbÃ;N+ø`;UX"     À5ø{ X¯¨*lƒ@     àü5Pt÷pPF@-;VX"     À5øk X;ĞQ_øùÊhÇz k#P     ¸JùùÅ:¾Ğtìè¨ô]ğµcQ€•(     \¥¿¿?±F£$éx =ªlƒ@     à*>m($I[Q(VÒªU+9²Ü¶ª[·n•:ßâÅ‹åëËšz    °¥¿¯P,
¼¸Bq·TìfŠ ë#PpE7n”Á`Ğùóçí]
    \Wş(:8H¹.ÿk°GE€õ9Ù» àj­ÑS6ïÍ±é|×‹ÂÂÂ+w   €Ô_Åš~NúÍÉÏt¼;@ºég{TX+ùâ‹/äãã£ÄÄÄ2mk×®Õ=÷Ü#___ùùùéĞ±cÇLíå­LJJ’Á`Ğ‰'ÌÆZ½zµ"##åêêª¸¸8:uÊ¬ı³Ï>SóæÍåêêª[n¹E'NTQQ‘©İ`0höìÙzğÁåáá¡¡C‡ªuëÖ’¤š5kÊ`0hàÀ•ğ‰    @ÕVRbTFÆÓqÍ@åÉÕt¼¡¶dt´Ge€u(6ğá‡ªwïŞJLLTß¾}Ë´çææjÔ¨QÚµk—Ö¯_/uïŞ]%%%š'//O“&MÒÒ¥KµmÛ6?^=ö˜©}Ë–-züñÇõÜsÏéàÁƒš3g/^¬I“&™¯îİ»ëÇÔÄ‰õñÇK’RRR”––¦3f\Å§     ÕËÙ³**2š=ÍÓÃ?¥¬Ú¶®
°>y¬ì½÷ŞÓ¿şõ/ı÷¿ÿÕı÷ß_nŸ‡~ØìxáÂ…ò÷÷×Áƒkñ\………z÷İwuûí·K’–,Y¢èèhığÃjÙ²¥&Nœ¨qãÆiÀ€’¤[n¹E¯½öš^xáM˜0Á4NŸ>}4hĞ Óqjjª$)  €_    àÿü}C§ ²1ËÁ éÎÓ¶ª°EÀŠV­Z¥ŒŒmÛ¶M·İvÛ%û9rDãÇ×;tæÌÓÊÄ“'OV(Ptrr2›§AƒòõõÕ¡C‡Ô²eKíÛ·OÛ¶m3[‘X\\¬üü|åååÉİİ]’Ô¢E‹ŠŞ*    Üp~ıõO³cc`Ùç›¿”îøQ2Øª(À+jÖ¬™öìÙ£…ªE‹2Êÿ'¤k×®
Ó¼yó¬’’ÅÆÆª  @’äàpñíFãÿ–Ò_Íf)999š8q¢zôèQ¦ÍÕõïùğğğ¨ğØ    PİuôhÈÔ™úí7óŠAeÅ_\¥©F¦­ª¬@°¢ºuëjêÔ©jÕª•õî»ï–ésöìY¥¤¤hŞ¼yº÷Ş{%I[·n5ëãïï/IJKKSÍš5%]Ü”åïŠŠŠ´k×.µlÙRÒÅw?^ÑÑÑ’¤æÍ›+%%EõêÕ«Ğ}¸¸¸Hº¸š    n$üQ 2•œœ¥Ã‡³”Ÿ_ş»î]]tÎÇ³Ü¶R"ªEÀÊê×¯¯ï¾ûN­Zµ’“““¦OŸnÖ^³fMùùùiîÜ¹ªS§N<©qãÆ™õ©W¯BBB¯I“&é§Ÿ~ÒÔ©SËÌåìì¬gŸ}V3gÎ”“““†®;î¸Ã0?^<ğ€BCCÕ³gO988hß¾}:pà€^ıõKŞCXX˜ƒÖ¬Y£Î;ËÍÍMåÿC	    UYQQ‰ÉQrr–ÈTZZş•/’äè ßT«Ü¶mRÔ‘Ê¬°/vyl **J6lĞ²eË4zôh³6-_¾\»wïVll¬şy½ıöÛf}œµlÙ2>|X7Ö[o½Un èîî®±cÇªOŸ>ºûî»åéé©+V˜Úãââ´fÍ}óÍ7ºí¶ÛtÇwèwŞQXXØeë¿é¦›Lºjøğá×ği    ÀõåìÙÚ´éw½ÿşQµOÓ§Ñºu§-%É'ÈQÆKÄ,?øH%5*«ZÀşÆ¿¾”°“¬¬,ùøø(33SŞŞŞ¦óùùùJMMUDD„Ù;ş ká{   ¨ş
/®B<p SfU(8¼”æzhO——lß'Õ9õ—÷Hò½æimêR¿»ãÆÃ#Ï      Ú;sæÂÿm¦’¥Ÿ~ÊÖ…å¿ñj9”³!Ë_íü[ TaŠ      Úùë*Ä2uúô«ÎWxùˆeCm©“ƒd¨Ü°E     P-üşûS€øÓO9*(°Mzg0Hy—X²¤?Éëw›”X"     ¨’
K”’’mÚ‘9#Ãº«/Å·–£Ò]ü®Øïp t"ªE     PeddäëÀ,%'g*%%[……ößk¶V £ÉıŠıÖJ·°AA€•(    €ëVAÁÅUˆd*99K¿ÿnŸUˆ—ãxùYJ¥ºI^’K¶•¬Œ@     \WÒÓóMâ‘#×Ç*ÄËq¶0P”¤“R=ETqŠ     À®òó‹uøp¶’“/†ˆgÏØ»¤Š	²<^ùR½£V¬°E     `s¿şú§iâ±c9**º¾W!^Î…
¬PüŞWêç"9X±ÀÚøş®SƒA«W¯¾ªk[µj¥‘#GVrE–	×ôéÓí2ö‰'d0”””d•ù   T½{ÏéÕWê“O~UJJv•]\:_ÓÍâş%éL€l€@ÀugñâÅòõõµw    ¬$'§ÈŞ%TšÚJ3Ô®Ğ5IŠ¨âxäUÖSOí¶é|sæÜjÓù     ºªN¢w £~“å<KÒ·RÛ<Uğ*àúÁ
EÀŠV­Z¥FÉÍÍM~~~j×®rssµsçNµoß^µk×–î¿ÿ~íÙ³ç²cM˜0AuêÔÑşıû%Iï¿ÿ¾"##åêêªÀÀ@õìÙÓ¬II‰^xáÕªUKAAAŠ7kŸ6mš5j$…„„èé§ŸVNN©½t•àš5k%wwwõìÙSyyyZ²d‰ÂÃÃU³fM1BÅÅÅfcggg«wïŞòğğĞM7İ¤÷Ş{Ïâ¹7nÜ¨Aƒ)33SƒAƒÁ¬ö¼¼<<X^^^
ÕÜ¹s/ù™kÈ!Šˆˆ›››¢¢¢4cÆŒË~Î    ¬/7·øÊª·
¼?±T¦“ô›—Šl„@°’´´4õîİ[ƒÖ¡C‡´qãFõèÑCF£QÙÙÙ0`€¶nİªíÛ·+22R;wVvvv™qŒF£}öY-]ºT[¶lQãÆµk×.1B¯¾úªRRR´víZİwß}f×-Y²DÚ±c‡&O¬W_}UëÖ­3µ;88hæÌ™JNNÖ’%K´aÃ½ğÂfcäååiæÌ™Z¾|¹Ö®]«7ª{÷îúòË/õå—_*!!AsæÌÑªU«Ì®{ûí·Õ¤Iíİ»WãÆÓsÏ=gñÜwİu—¦OŸ.ooo¥¥¥)--McÆŒ1];uêTµhÑB{÷îÕÓO?­üãJII)÷¿AII‰n¾ùf­\¹RÔøñãõÒK/é£>²ğ¿"    k¨N++°Ãó_*¹À†xä°’´´4©G
“$5jÔH’Ô¦M³¾sçÎ•¯¯¯6mÚ¤xÀt¾¨¨HıúõÓŞ½{µuëVİtÓM’¤“'OÊÃÃC<ğ€¼¼¼¦fÍš™Ù¸qcM˜0A’©wß}Wë×¯Wûöí%ÉlÓ–ğğp½şúë6l˜Şÿ}ÓùÂÂBÍ=[uëÖ•$õìÙS			:}ú´<==Õ°aCµnİZß}÷}ôQÓuwß}·Æ'Iª_¿¾¶mÛ¦wŞyÇ¢¹]\\äãã#ƒÁ    2ŸkçÎõôÓOK’Æ«wŞyGß}÷¢¢¢ÊôuvvÖÄ‰MÇúşûïõÑG©W¯^eú   °ê(_Å
E ªc…"`%Mš4QÛ¶mÕ¨Q#=òÈ#š7oÎ;'I:}ú´†ªÈÈHùøøÈÛÛ[999:yò¤ÙÏ?ÿ¼vìØ¡Í›7›ÂDIjß¾½ÂÂÂtË-·¨ÿşJLLT^^Ùµ76;®S§222LÇß~û­Ú¶m«›nºI^^^êß¿¿Î=k6»»»)L”¤ÀÀ@…‡‡ËÓÓÓìÜ_Ç•¤;ï¼³Ìñ¡C‡*4÷¥üõ¾JCÇ¿ÏÿWï½÷n½õVùûûËÓÓSsçÎ-ó9   °­ê(æ²V7EÀJµnİ:}õÕWjØ°¡fÍš¥¨¨(¥¦¦jÀ€JJJÒŒ3ôÿşßÿSRR’üüüTPP`6Fûöíõë¯¿êë¯¿6;ïåå¥={öhÙ²eªS§Æ¯&Mšèüùó¦>ÎÎÎf×•””H’Nœ8¡x@7ÖÇ¬İ»w›Şsø×ÊãrãZÂÒ¹/¥"ó/_¾\cÆŒÑ!CôÍ7ß())Iƒ²h    Ö“›[=E_Ge¸Ö²w€Í£Vd0t÷İwëî»ïÖøñã¦O?ıTÛ¶mÓûï¿¯Î;K’N:¥3gÎ”¹şÁT×®]Õ§O9::ê±Ç3µ999©]»vj×®&L˜ ___mØ°A=zô¸b]»wïVII‰¦N*‡‹ÿ_¡2ß+¸}ûö2ÇÑÑÑÏíââRf£—«±mÛ6İu×]¦G¤%éØ±c×<.   €kS]V(Ö
tPª<¯Ü¨f+Ù±c‡Ö¯_¯:(  @;vìĞï¿ÿ®èèhEFF*!!A-Z´PVV–şùÏÊÍÍ­Üqºwï®„„õïß_NNNêÙ³§Ö¬Y£ãÇë¾ûîSÍš5õå—_ª¤¤¤Ü÷–§^½z*,,Ô¬Y³ÔµkWmÛ¶M|ğA¥İû¶mÛ4yòduëÖMëÖ­ÓÊ•+õÅ_X<wxx¸rrr´~ız5iÒDîîîrww¯p‘‘‘Zºt©¾şúkEDD(!!A;wîTDDD¥Ü'   €Š+)1êÏ?«Ç.ÏW¹!PÕñÈ3`%ŞŞŞÚ¼y³:wî¬úõëëå—_ÖÔ©SÕ©S'-X°@çÎSóæÍÕ¿1B—«gÏZ²d‰ú÷ï¯O>ùD¾¾¾úä“OÔ¦MEGGëƒ>Ğ²eËcQmMš4Ñ´iÓôÖ[o)66V‰‰‰zã7*ëÖ5zôhíÚµKÍš5Óë¯¿®iÓ¦)..Îâ¹ïºë.6L>ú¨üıı5yòä«ªã©§R=ôè£êöÛo×Ù³gÍV+   °½ÜÜ"ö®âê9;cĞ}ş®ĞNÇí]`£±*ÿ£ºÈÊÊ’233åíím:ŸŸŸ¯ÔÔTEDDÈÕÕÕâFÁ÷   `]ééùš0!ÙŞeTHí '…ÅäªVì1Õ¨¿KÎ.rTŒ©•’U÷Ê”ãEIá•Z¥õ]êwwÜxX›     l¦*¼?ÑÙÙ ğ(©NÌiyÆî“cÀ	¹)LyŠÕqÔ!…ÉÈCŸ¸(     ›¹^Eÿ@'…ÆäÊ/öˆ\êï‘‹³³ÔPj£ÕPçØ|0!P     6“›{}Š..W!Å¤Ë+v¯üOÉMáÊU¬éyV(«K P     6cÏŠşN
‹Í‘_ìQ9G–®BŒÑiµ×~5T&«‹(     ›±e èRÃAQFÅ¤Ë3v¯kÿ"WE(W1:¢QúI¡2Ê`³z€ê‚@U›‘ÃVø^   ¬ËÚ<ÔqRXL¶jÅ•s½İrqv•ƒb”®úQ•)«ÎÜq]svv–$åååÉÍÍÍÎÕàF——'éß{    *We¯P¬QÃAáŒª“&Ø½rôûU®ŠPbuDÿÔO
a"PÉq]stt”¯¯¯222$Iîîî2ø‡ •Ïh4*//Oòõõ•£££½K   ª¥Êë8)46[µbÈ%r\œÜ$Å(]ô£¢•%÷k/À%(âº$I¦P°&___Ó÷   €Ê—›[\ák\]Ş DA1iòˆİ#ÇZirÕ-ÊQ¬Rô‚èfV!6D ˆëÁ`P:u ÂÂB{—ƒjÌÙÙ™•‰   €•YºB1(ØI¡±Yª{DÎõv«†£§¤¥©‹ö«¡rÄk± {!PD•áèèHØ    UØÅW•(º¹9("ºD1¿Ê3fjfÈUu•­VÕÍ6®À¥(     ›ÈË+VIÉÅ?Rpˆ“BbÎË'ö°œ#ö©†£ŒŠQšÒ~5P.«ë"     °‰ÒÇÃê:*vØJ9yŸWE*K1JVW¥*ØÎ°"     °‰ÜÜ‹¢W€£~÷~Lû¥|¹Ø¹* E      l¢t…b‰§»~P];Wàj9Ø»      pcÈÍ-–$9x²á&P•(     ›(]¡(E *#P     6Q(Y¡TiŠÕØæÍ›ÕµkWË`0hõêÕ¦¶ÂÂB;V5’‡‡‡‚ƒƒõøãë·ß~3ã?şPß¾}åíí-___2D999f}öïß¯{ï½W®®®
		ÑäÉ“mr    €ª¥4P,f…"P¥(Vc¹¹¹jÒ¤‰Ş{ï½2myyyÚ³g^yåíÙ³GŸ|ò‰RRRôàƒšõëÛ·¯’““µnİ:­Y³F›7oÖ“O>ijÏÊÊR‡¦İ»wëí·ßV||¼æÎkõû    T-¥»<zG U»<Wc:uR§NÊmóññÑºuëÌÎ½ûî»jÙ²¥N<©ĞĞP:tHk×®ÕÎ;Õ¢EIÒ¬Y³Ô¹sgM™2EÁÁÁJLLTAA.\(ÅÄÄ())IÓ¦M3    (]¡XÀ#Ï@•Æÿ€Iff¦ƒ|}}%Ißÿ½|}}Ma¢$µk×NÚ±c‡©Ï}÷İ'SŸ¸¸8¥¤¤èÜ¹s¶½    Àu­t…âŸ<òTi¬P„$)??_cÇUïŞ½åíí-IJOOW@@€Y?'''ÕªUKééé¦>f}Mm5kÖ,w¾.èÂ…¦ã¬¬¬J»    Àõ)'§HnnÊvt·w) ®+¡ÂÂBõêÕKF£Q³gÏ¶Éœo¼ñ†|||L_!!!6™    `?¹¹År÷tÔ9Û» ×€@ñW&şüóÏZ·niu¢$)##Ã¬QQ‘şøã™úœ>}Ú¬OéqiŸò¼øâ‹ÊÌÌ4}:uª²n	    púóÏbåêATuüßÀJÃÄ#GèÛo¿•ŸŸŸYûwŞ©óçÏk÷îİ¦s6lPII‰n¿ıvSŸÍ›7«°°ĞÔgİºuŠŠŠºäãÎ’T£Fy{{›}    ª¯ÒYj°!På(Vc999JJJRRR’$)55UIII:yò¤
Õ³gOíÚµK‰‰‰*..VzzºÒÓÓUPP IŠVÇ5tèPığÃÚ¶m›†®Ç{LÁÁÁ’¤>}úÈÅÅEC†Qrr²V¬X¡3fhÔ¨Qv»o    Àõ§tCgE ÊcS–jl×®]jİºµé¸4ä0`€âããõùçŸK’š6mjvİwß}§V­ZI’5|øpµmÛVzøá‡5sæLS_}óÍ7zæ™gtë­·ªvíÚ?~¼|òI+ß     *)]¡èÈÏ@•G XµjÕJF£ñ’í—k+U«V-}øá‡—íÓ¸qcmÙ²¥Âõ    n¥¢+*G    €Õ•Š6eª<~Š    €Õ•¾C±„Š@•G      ¬®t…b1"På(     «ËÍ-–$yE U?Å     ÀêJW(^`—g Ê#P     VWúÅNv®Àµ"P     V—“S¤5”íäjïR \#E     `u¹¹Er÷tĞŸªaïR \#E     `U.«°Ğ(7ŞŸTŠ     ÀªJ7dqñ$PªE     `U¹¹Å’ê‚@     XUé
E'b :à'     XUi èÀ
E Z P     VU(ŠMY€j@     XUnîÿŠÄ@uÀO2     °ªÒŠÅ<òTŠ     ÀªJW((Õ"     °ªÒŠ¼C¨   üöî<Ê®²@÷[Sf*a0	éˆMºc ]­F£ˆ¤ÛË2·›®iô"¨ÚNÖEiQî]q€üIh¡EP"`e ÂÔ†	DÆT$©¢*Sç÷G¥
Ê0THUíS§g­½Î9{ï“ınÅ¢Ş|ßş úUW¡¸ÅE¨
E    _µ´´§®®*/^t (  €~ÕÜÜ–ÑcjÒ’‘EGú€B   è7­­Ùºµ##Mw†Š¡P   úM×ó‡VA@¥ğÓ   ô›®B±ÎE¨
E    ßtŠµ£ŠP)Š   @¿iié,kÆ¨  Røi   úM×Å˜òC¡   ô›––öÎ7
E¨
E    ßtPl÷E¨
E    ßtŠm
E¨
E    ßt-Ê²Õ”g¨
E    ßtPÜ¢P„Š¡P   úMKK[jk«²qÄ°¢£ }D¡   ô›ææ¶Œ]—2²è(@Q(   ı¢½½”Í›;2rLMJ©*:ĞGŠ   @¿èz~â+<CEQ(   ı¢«PfA¨(
E    _tŠµcÔPIüD   ı¢¥¥³P¬6å*ŠB   è]#«Ly†Š¢P   úEW¡S¡¢ø‰   úE×”çöÑµ'ú’B   è]#ÛMy†Š¢P   úEKK{’d«)ÏPQüD   ı¢k„â«<CEQ(   ı¢¥¥-ÕÕÉÆQuEGúB   èÍÍm5º&MUt )  €>×ÑQÊ¦Mí5¦&%õT?Ñ   @ŸknnK©”Œ­z€Jã§   ès--²cA¨4
E    Ïµ´´'IjŠPqŠ   @Ÿknî¡X3Fõ •ÆO5   Ğçº
ÅªÑµ'úšB   ès]…b,ÊÇO5   ĞçºeéğE¨8
E    ÏuPlS(BÅQ(   }®k„b«B*B   ès]#·ŒV(B¥±Ô   1¥´·÷ï¶ví–TU%›F© Òø©  €~ôøãÍiiiëQ¶µµõ¾˜Û™òïµ®S*Ì½]“¦êQs1`À(   ¬ZÕ’k®y&+W6¥0£ÆT§%¦<C¥Q(  @zá…-Y¸ğÙÜsÏú	X®†[*’B   ú@KK[şó?Wç¶ÛO[Ûo·Q(BeR(  ÀNhkëÈoû|n¼quZZÚ‹SVê¬ğI¡   oÒİw¯ËÂ…Ïæ…¶¥,ÕŒ®.:ĞŠ   °ƒV®lÎ5×<“U«ZŠRÖªLy†Š¤P  €^Z³fs~ñ‹gsÿıŠ28(¡")  à47·åúëŸËï~÷BÚÛ-¸Ò[%…"T$…"   ƒF©TÊ³ÏnÊc5ç±Ç^ÊúõóìÂ††ÍÙ¼¹c@®UIÚ,ÊI¡  @Ùêèè,}ô¥¬XÑœ+^²’ò Òj„"T$Ë-U°Ûo¿=ıèG3iÒ¤TUUeáÂ…=—J¥Ì›7/{î¹gF™3fdÅŠ=ÎY·n]fÏúúúŒ7.§œrJš››{œ³lÙ²¼÷½ïÍˆ#²×^{å‚.è÷{  *SGG)O=µ17İ´&?üáÊœuÖ9ï¼å¹úêgrÿı”‰ƒÌV«<CE2B±‚µ´´ä]ïzWN>ùäœpÂ	Û¿à‚2şü,X° “'OÎÜ¹ssôÑGç‘GÉˆ#’$³gÏÎêÕ«sÓM7¥µµ5ŸúÔ§2gÎœ\yå•I’¦¦¦uÔQ™1cF.ºè¢<øàƒ9ùä“3nÜ¸Ì™3g@ï  |º
ÄÇ{)COv    IDAT=Öœ•+›³i“Ò°TU%›P„ŠTU*•<Mv¨ªªÊµ×^›ã?>IçèÄI“&å¬³ÎÊ¿øÅ$Iccc&L˜Ë.»,'xb–/_<0wß}w¦Nš$Y´hQ>üáç™gÉ¤I“rá…æ+_ùJ2lØ°$ÉÙgŸ…æüc¯ó555eìØ±illL}}}ß=  P.ÚÛ_Y ¾”•+›=›°BUöo”­©+:JY:'É>E‡ØA~w§‹ŠCÔªU«ÒĞĞ3ftï;vl¦M›–%K–äÄOÌ’%K2nÜ¸î21IfÌ˜‘êêê,]º4ÿøÇ³dÉ’¼ï}ïë.“äè£Î7¿ùÍ¬_¿>»îºë€Ş  P^ÚÛKyòÉ–îET\8TŒS“•‰P‘ŠCTCCC’dÂ„	=öO˜0¡ûXCCCÆßãxmmmvÛm·çL<y»?£ëØkŠ[¶lÉ–-[º?755íÄİ   å¢½½”U«ZºG >ñDK¶lQ E#¬ğK¡H!Î?ÿüœ{î¹EÇ   vR[[Ç¶±¹»@ÜºUH2Ü‚,P±ŠCÔÄ‰“$kÖ¬É{îÙ½Íš59ôĞC»ÏY»vmïµµµeİºuİßŸ8qbÖ¬YÓãœ®Ï]ç¼šsÎ9'gyf÷ç¦¦¦ìµ×^;qG  À@èèè¸|yS{¬9O<ÑœÖVæg{udŠ¥P¢&Oœ‰'æ–[né.›šš²téÒ|æ3ŸI’LŸ>=6lÈ½÷Ş›Ã;,Irë­·¦££#Ó¦Më>ç+_ùJZ[[SW×ùlŒ›nº)ïxÇ;^÷ù‰Ã‡ÏğáÃûó €>²iS{y¤)Ë–mÈƒ6¦¥Å*Ì¼±ZS¡b)+XsssV®\ÙıyÕªU¹ÿşû³Ûn»eï½÷Îgœ‘óÎ;/ûï¿&Oœ¹sçfÒ¤Iİ+ApÀ9æ˜còéO:]tQZ[[súé§çÄOÌ¤I“’$Ÿüä'sî¹çæ”SNÉÿşßÿ;=ôP¾ûİïæÛßşv!÷  ôçŸß’eË6dÙ²Æ¬XÑœöv£Ù1UF(BÅR(V°{î¹'ïÿû»?wM1>é¤“rÙe—åË_şrZZZ2gÎœlØ°!Gyd-Z”#FtçŠ+®Èé§Ÿ~ğƒ©®®Î¬Y³2şüîãcÇÍo~ó›œvÚi9ì°Ã²Ç{dŞ¼y™3gÎÀİ(  °Ó::Jyâ‰–<ğ@ç(ÄÕ«7‰ÁN¡«ªT*ùk&
×ÔÔ”±cÇ¦±±1õõõEÇ €!aÓ¦ö<üpc|°ÑTfúÜ”š˜‡ÿò/ŠQ¶ÎI²OÑ!vßİéb„"  Àb*3¥İ3¡b)  *˜©Ìô•ªª¤¦¦*Õ5U©©é|_S“To÷¾”êš¤c7e5T*…"  @…éšÊ¼lYczÈTæ¢UW'µµUÛJ·ªm¥[RSûòûêê—‹¸ê¯¥ÔÔ”Rµí}çÖ±ísÇ¶÷şÚêš¤¦=Õµí©ªyyKM{ªkÚ“š¶mŸÛRµmK×Ö¤¦-ÙöZUÓšTïÈ¿GUy4_é·¦@±Š   àùç·ä:§2¯\i*s96¼:‡}hCv?êúToéÅ7ªR•šT§&U¶¥Çku²í}×Vú³÷/oµİ¯RjÒ±íXÇ+Ş·¿âsû+^_mk{Åk[jÒúŠ÷]ŸK©î·®@ñŠ   ƒPGG)?ŞœeËMe.3ÕÕÉ!‡·å¿wCFŸ?æÔ4gt®s«íQÂu(á€AB¡  0H˜Ê\şö?¨*ûÏúmFOÚ”ÇóÉÜ—ŠĞçŠ   eÌTæÁaÒ^µ9xÖ}sÀ£y!Ë™fÚ/P±Š   eä•S™—-kLCƒ©ÌålÜ®5y÷ÇVeì{nÏÖª£rCfgK† _)  
¶iS{z¨³@|øaS™ƒ#ªó—Ç¬Ëî3nHmİ_gq¾šuÙ¥èX B¡  P€µk7wB4•yğ¨®N}ßÖLúÈ¹ËË]9;OfBÑ± ”B  ` tt”²res|ĞTæÁê‡&o;áæŒ™Ğ‘åùÇ<˜ı‹P…"  @?1•¹ÿŒÛµ&ûLÙ’Q{4Èõê÷<cö{29>7fjJ©ë”#…"  @zåTæ+^JGGÑ‰*CmmUŞºU&My>»LY–ÚIgDöJ)»Èõ_Ê~ùeNN«_£ü—  `gtMeî,7dÍš-EGª»íQ›}¦lÌîSÈˆwşWFß=[òö<—£óHöKKF`HR(  ì ÛòğÃM¦2÷±ººªìóödâ”µ©?è¡ŒĞ–Íy{Ë»²<³Ò’EG 
E  €^Y³¦s*óƒšÊÜ—ö_›·Ô’=¦¬Ê˜·¿­ÃöÎsyGîÍßd£ ,)  ^…©ÌıcØ°ªìóÎQˆãz1¥·ŒÎ³yGgj6gXÑñ è…"  À6¯œÊüĞCÙ¸ÑTæ¾0aÏÚì5¥%»Ô’ÚıZó\İÛ²4È"À ¤P  †´®©ÌË–mÈÊ•Í¦2÷#ª³Ï;“=¦´eøA[óÂn{äÁ˜-©+: }@¡  )íí¥<ş¸©Ì}mÏÿV—	ÖdÄAÕiŞoX«Ù;T T$…"  Pñ6nlËC5eÙ²yøá&S™ûÀÈQ5ù‹wÊˆƒFfó”º<9n¬ö+&Àà¿ö  @E2•ùÍ©ªJF©Í¨úºŒ¨¯Mİ.u©®¯MêëÒQ_—Ö]j³el]Ÿ4"+kª‹@Š  @E0•ùµUW'£w©Ë¨úÚß¥.uõµ©®¯ë,	w©ÍÖúºl©¯ÍÆúº´Œ©MsuUš‹@ÙR(  ƒÖPÊ\SS•Ñ»t$^_›ÚWŒ$lß6’ps}]6Ö×¦eLm^ªªÊKE‡ "( €A¥’§2×ÖVeL}]FnIX[_›ªm#	Û·$Ü¼m$áÆQ5iªªJSÑ¡rŠ  @Yko/eåÊæ,[¶!>Ø8è¦2VÑõµ¹K]†Õ×¦¶¾.Õõu)íR›¶úºl­ïIØR_—Í#k²!É†¢CÀëP(  e§¥¥-?\¾S™‡¨ÎèúºŒÜ¥6ÃêëR[_—ª]jÓQ_—¶úÚ´Ö×eS}]šw©ÍÖ5Ùšd}Ñ¡ ( €²PôTæ#kºGÖmIXµmÑ’¶m‹–lÚ6’pË°ê®q’ ĞwŠ  @!^9•yÙ²Æ¬]Û÷İ¨Ñ5U_—ÛFÖ¼b$ak}ç3	7nI¸¹®:›û< T…"  0`vv*sUU2jLmFïR›áõuV_—ê]:W6î¨ïIX—»Ô¦¹¾.kª²±Ÿî †*…"  Ğ¯6wB|üñí§2WW'£ÇÔfT}]†×wN7®Ş¥.©ïIX—Í»t®lÜ²KmZª«ÒRÌ­  Q(  }¬½½”•«Zòèª–¬úSKš7µ§®¾.5“GgßwKû¶‘„›êë²±¾6-£kóRuU^*:8 Ğ+
E   ÏÜäšR)öì7¦è8 @?P(  ;­1É•IîO’ÚêbÃ  ıJ¡  ì”%I~–Xü †…"  ğ¦¬OrE’‹ (…"  °Ã~Ÿäš$›Š 8…"  Ğk/&ù÷$Ë‹ F¡  ¼¡R’Û“ü"Éæ‚³  ÅR( À·9É°$¯µ6óóé•øè€% Ê™B  †¸ÿJ²0É‘IŞ—dì¶ı¥$¿İvlK1Ñ €2¤P €!n]’InHòŸIM25É­IV˜ (O
E  âÖ½â}G’û¶m  ¯æµ“  ëŞø €n
E  âŠ ÀP(–±Í›7 €!`}Ñ €AE¡Xf:::òµ¯}-ñ‘1cÆä‰'H’Ì;7?úÑ
N @¥iœ€£P,3çw^.»ì²\pÁ6lX÷şƒ:(—^ziÉ  ¨D¦; ;J¡Xf.¿üò\|ñÅ™={vjjjº÷¿ë]ïÊÿøÇ“ P‰Š ÀR(–™gŸ}6ûí·ßvû;::ÒÚÚZ@"  *™B ØQ
Å2sàæw¿ûİvû¯¹æš¼ûİï.   •L¡ ì¨Ú¢ĞÓ¼yórÒI'åÙgŸMGGG~ñ‹_äÑGÍå—_n¸¡èx  T…" °£ŒP,3ûØÇrıõ×çæ›oÎèÑ£3oŞ¼,_¾<×_}>ô¡ €
£P v”Šeè½ï}onºé¦¢c  0(€e„"  QmIšŠ :F(–]wİ5UUU½:wİ:‡ @ßXŸ¤Tt `ĞQ(–ï|ç;İï_|ñÅœwŞy9úè£3}úô$É’%Kòë_ÿ:sçÎ-*"  È_U oFU©Tò—’edÖ¬YyÿûßŸÓO?½Çşïÿû¹ùæ›³páÂ‚’õ¯¦¦¦Œ;6©¯¯/: À°$ÉeE‡ ¢ÎI²OÑ!vßİéâŠeæ×¿şu9æ˜íösÌ1¹ùæ›H @¥2B x3Šef÷İwÏ/ùËíöÿò—¿Ìî»ï^@"  *•B x3<C±Ìœ{î¹ùŸÿóæ¶ÛnË´iÓ’$K—.Í¢E‹rÉ%—œ €J¢P Ş…b™ùÇüÇpÀ™?~~ñ‹_$I8à€üş÷¿ï.  /¬/:  0()ËĞ´iÓrÅW €
g„" ğf(ËP{{{.\˜åË—'I¦L™’ã;.555'  R´$ÙRt `PR(–™•+WfæÌ™yæ™gòw¼#Irşùçg¯½öÊ¯~õ«ì»ï¾'  ¼Xt  `Ğ²Ês™ùüç?Ÿ·½ímyúé§sß}÷å¾ûîËSO=•É“'çóŸÿ|Ññ  ¨¦; o–ŠefñâÅ¹óÎ;³Ûn»uïÛ}÷İóo|#GqDÉ  ¨$
E àÍ2B±Ì></½ôÒvû›››3lØ° P‰Š À›¥P,3ùÈG2gÎœ,]º4¥R)¥R)wŞygN=õÔwÜqEÇ  B(€7K¡XfæÏŸŸ}÷İ7Ó§OÏˆ#2bÄˆqÄÙo¿ıòİï~·èx  T…" ğf)ËÌ¸qãòË_ş2=öX®¹æš\sÍ5yôÑGsíµ×fìØ±}z­öööÌ;7“'OÎÈ‘#³ï¾ûæk_ûZJ¥R÷9¥R)óæÍË{î™‘#GfÆŒY±bE?gİºu™={vêëë3nÜ¸œrÊ)innîÓ¬  ô­õE  -‹²”©ıöÛ/ûí·_¿^ã›ßüf.¼ğÂ,X° S¦LÉ=÷Ü“O}êS;vl÷ŠÒ\pAæÏŸŸdòäÉ™;wn>úè<òÈ#1bD’döìÙY½zunºé¦´¶¶æSŸúTæÌ™“+¯¼²_ó ğæ´'i,: 0h¡XffÍš•o~ó›Ûí¿à‚òw÷w}z­;î¸#ûØÇ2sæÌì³Ï>ùÛ¿ıÛuÔQ¹ë®»’tNüÎw¾“ù—ÉÇ>ö±rÈ!¹üòËóÜsÏeáÂ…I’åË—gÑ¢E¹ôÒK3mÚ´yä‘ùŞ÷¾—Ÿşô§yî¹çú4/  }c}’Ò ğêŠeæöÛoÏ‡?üáíö{ì±¹ıöÛûôZ‡~xn¹å–<öØcI’x ¿ÿıïsì±Ç&IV­Z•†††Ì˜1£û;cÇÍ´iÓ²dÉ’$É’%K2nÜ¸L:µûœ3f¤ºº:K—.íÓ¼  ôÏO v†)Ïe¦¹¹9Ã†Ûn]]]šššúôZgŸ}všššòÎw¾3555iooÏ¿şë¿föìÙI’†††$É„	z|oÂ„	İÇ2~üøÇkkk³Ûn»uŸój¶lÙ’-[¶tîë{ àµ)€a„b™9øàƒsÕUWm·ÿ§?ıi<ğÀ>½ÖÏ~ö³\qÅ¹òÊ+sß}÷eÁ‚ùÿïÿeÁ‚}zWsşùçgìØ±İÛ^{íÕï×  Ó‹E  5#ËÌÜ¹ssÂ	'äñÇÏ>ğ$É-·Ü’ÿøÿÈÕW_İ§×úÒ—¾”³Ï>;'xb’Î2óÉ'ŸÌùçŸŸ“N:)'NL’¬Y³&{î¹g÷÷Ö¬Y“C=4I2qâÄ¬]»¶ÇŸÛÖÖ–uëÖuÿÕœsÎ99óÌ3»?755) ˆŠ ÀÎ0B±Ì|ô£ÍÂ…³råÊ|ö³ŸÍYg•gy&7ß|s?şø>½ÖÆS]İó_šššttt$I&Oœ‰'æ–[né>ŞÔÔ”¥K—fúôéI’éÓ§gÃ†¹÷Ş{»Ï¹õÖ[ÓÑÑ‘iÓ¦½æµ‡úúú  C¡ ì#ËĞÌ™33sæÌ~¿ÎG?úÑüë¿şköŞ{ïL™2%ÿõ_ÿ•o}ë[9ùä““$UUU9ãŒ3rŞyçeÿı÷ÏäÉ“3wîÜLš4©»Ü<à€rÌ1ÇäÓŸşt.ºè¢´¶¶æôÓOÏ‰'˜I“&õû=  °ãÖ  ÔŠCØ÷¾÷½Ì;7Ÿıìg³víÚLš4)ÿôOÿ”yóæuŸóå/9---™3gN6lØ#<2‹-Êˆ#ºÏ¹âŠ+rúé§çƒü`ª««3kÖ¬ÌŸ?¿ˆ[  ŒP vFU©T*ššš2vìØ466šş ĞZ’œù†gĞßÎI²OÑ!vßİéâŠ  0„ ì,…bhjj*:  C„B ØY
Å2°ë®»fíÚµI’|àÙ°aCÁ‰  ¨T
E `g)ËÀ˜1còâ‹/&In»í¶´¶¶œ €J¥P v–UËÀŒ3òş÷¿?p@’äãÿx†öªçŞzë­ €
£P v–B±üä'?É‚òøãgñâÅ™2eJFUt,  *Ğ‹E  =…b9rdN=õÔ$É=÷Ü“o~ó›7n\Á©  ¨Dë‹  z
Å2óÛßş¶û}©TJ’TUU €A¬=ÉÚ$¯ØM T…bºüòËóÿïÿÍŠ+’$oûÛó¥/})ÿ÷_p2  ÊÑÆ$«óri¸fÛëóI:
Ì T&…b™ùÖ·¾•¹sçæôÓOÏG‘$ùıïŸSO=5/¼ğB¾ğ…/œ €"t¤sA•†WÙ^*0 0ô(ËÌ÷¾÷½\xá…ù‡ø‡î}Çw\¦L™’ÿóşB  ÂmÍö…ášm[k¹  º(ËÌêÕ«søá‡o·ÿğÃÏêÕ«H @hÌ«6\Ÿ¤T`. €7¢P,3ûí·_~ö³ŸåŸÿùŸ{ì¿êª«²ÿşû”
 `èéHçhÁµ>¯>âpSüÙ  EP(–™sÏ=7ÿı¿ÿ÷Ü~ûíİÏPüÃş[n¹%?ûÙÏ
N 0t,HrgÑ!  ÊPuÑèiÖ¬YYºtiöØc,\¸0.Ì{ì‘»îº+ÿøÇ‹ 0$ü!ÊD €×b„b:ì°Ãò“Ÿü¤è  CÒsI~Zt €2f„"  l³5ÉÅé›g' T*…"  lse’ÕE‡  (s
E  H²dÛ ÀëS( 0ä­NòE‡  $Še®©©).ÌòåË‹ P‘º›¸¥è   ƒ„B±Ì|âŸÈ÷¿ÿı$É¦M›2uêÔ|âŸÈ!‡’Ÿÿüç§ ¨<W¥seg  zG¡Xfn¿ıö¼÷½ïM’\{íµ)•JÙ°aCæÏŸŸóÎ;¯àt  •å®$¿/: À £P,3Ùm·İ’$‹-Ê¬Y³2jÔ¨Ìœ93+V¬(8 @åX“ä'E‡  „Šef¯½öÊ’%KÒÒÒ’E‹å¨£J’¬_¿>#FŒ(8 @ehç& ¼YµE §3Î8#³gÏÎ˜1c²÷Ş{çoşæo’tN…>øàƒ‹ P!~–ä™¢C  R
Å2óÙÏ~6ı×§Ÿ~:úĞ‡R]İ9ˆômo{›g( ô{’Ü^t €AL¡X†¦NšC9$«V­Ê¾ûî›ÚÚÚÌœ9³èX  ƒŞóIş½è  ƒœg(–™7æ”SNÉ¨Q£2eÊ”<õÔSI’Ï}îsùÆ7¾Qp: €Á«ë¹‰›‹ 0È)ËÌ9çœ“x ·İv[EXfÌ˜‘«®ºªÀd  ƒÛ5I*: @0å¹Ì,\¸0W]uUŞó÷¤ªªª{ÿ”)Sòøã˜ `ğº/ÉmE‡  ¨F(–™çŸ>ãÇßnKKK‚ €Şy>ÉåE‡  ¨ 
Å23uêÔüêW¿êşÜU"^zé¥™>}zQ±  ¥¶$—$ÙTt €
bÊs™ùú×¿c=6<òHÚÚÚòİï~7<òHî¸ã,^¼¸èx  ƒÊÏ“<Yt €
c„b™9òÈ#sÿı÷§­­-|p~ó›ßdüøñY²dI;ì°¢ã ÷'¹µè  ÈÅ2´ï¾ûæ’K.): À õb’E‡  ¨P
Å2ÔÑÑ‘•+WfíÚµéèèèqì}ï{_A©  ‡öt>7qcÑA  *”B±ÌÜyçùä'?™'Ÿ|2¥R©Ç±ªªª´··” `p¸6Éª¢C  T0…b™9õÔS»WzŞsÏ=»Wy à-KrsÑ!  *œB±Ì¬X±"×\sMöÛo¿¢£  *ë’\–¤ôç °s¬ò\f¦M›–•+W `PiOri’–¢ƒ  F(–™Ï}îs9ë¬³ÒĞĞƒ>8uuu=rÈ!% (_¿LòxÑ!  †…b™™5kV’ää“OîŞWUU•R©dQ €WñP’ß `Q(–™U«¬I Ğ[ë“ü8› 0Šeæ­o}kÑ  …t>7±¹è   CŒB±\wİu9öØcSWW—ë®»îuÏ=î¸ã( @y».‰¥ì  B±üñihhÈøñãsüñÇ¿æy¡ Ğé‘$‹Š 0D)Ë@GGÇ«¾ `{Iş¿xn" @Qª‹@ï<óÌ3™3gNÑ1  
ÕõÜÄ—Š 0„)‰_|1?úÑŠ P¨’<Vt €!N¡ À °<É  …"  å¯)› P.Š  ”µ$?Jg© @ñ¬ò\&N8á„×=¾aÃ†J P^nLòÇ¢C  ĞM¡X&Æû†Çÿáşa€Ò  ”‡ÇÒ¹  åC¡X&~üã  ¬¼”Î©ÎE  ÏP  ì”Ò¹‹‡¾  ”# è7mI¶îÀ¶eÛëóI) /  oL¡ ÀÛ”äÉWlëòrøÊÍte €Ê£P àumNòTzˆÏ§sZ2  CB €n[³}y¸&ÊC  ^¦P ¢Z“<åaCLS àõ) †€¶$Ï¤gyø\”‡  ì8…" @…iOòl¶/ÛŠ @ÅP( bé,_Y>å!  ıG¡ 0Ht¤ó‡O&ùS^.[Ì ÀĞ£P (C¥t®®ü§t‡O¥s•-f €D¡ P¸R’µy¹8ìzİ\d(  x
E €öBzN[~:ÉÆ" ÀP( ô£uy¹8ìyØRd   ØIÕE XÏ>ûlşÇÿøÙ}÷İ3räÈ|ğÁ¹ç{º—J¥Ì›7/{î¹gF™3fdÅŠ=şŒuëÖeöìÙ©¯¯Ï¸qãrÊ)§¤¹¹y o 
·>ÉıI~™ä{I¾˜äœ$ÿ–dQ’åQ& 0ø¡8„­_¿>GqDŞÿş÷çÆoÌ[Şò–¬X±"»îºk÷9\pAæÏŸŸdòäÉ™;wn>úè<òÈ#1bD’döìÙY½zunºé¦´¶¶æSŸúTæÌ™“+¯¼²¨[€~×”íG6  HU©T*‚bœ}öÙùÃşßıîw¯z¼T*eÒ¤I9ë¬³òÅ/~1IÒØØ˜	&ä²Ë.Ë‰'˜åË—çÀÌİwß©S§&I-Z”øÃyæ™g2iÒ¤^eijjÊØ±cÓØØ˜úúú¾¹A è#ÍéY>™dC‘ €Aïœ$ûbùİ.¦<a×]w]¦Nš¿û»¿Ëøñãóîw¿;—\rI÷ñU«V¥¡¡!3fÌèŞ7vìØL›6-K–,I’,Y²$ãÆë.“dÆŒ©®®ÎÒ¥K_óÚ[¶lISSS ÊAK’G’Ü˜ä¢tşÏşYéœÂ|]’¢L `h3åy{â‰'rá…æÌ3ÏÌ?ÿó?çî»ïÎç?ÿù6,'tR’$&Lèñ½	&tkhhÈøñã{¯­­Ín»íÖ}Î«9ÿüósî¹çöñÀÙ”£ŸLç
Ì  ÀkS(a™:uj¾şõ¯'IŞıîwç¡‡ÊE]”“N:©_¯}Î9çäÌ3ÏìşÜÔÔ”½öÚ«_¯	ÀĞ¶9ÏF    IDAT9|eyø|Ï~ €£PÂöÜsÏxà=öpÀùùÏ$™8qb’dÍš5ÙsÏ=»ÏY³fM=ôĞîsÖ®]ÛãÏhkkËºuëº¿ÿj†áÃ‡÷É} ÀŸÛšíËÃ5Q @_P(aGqD}ôÑû{ì±¼õ­oM’L<9'NÌ-·ÜÒ] 655eéÒ¥ùÌg>“$™>}z6lØ{ï½7‡vX’äÖ[oMGGG¦M›6€wÀPÕšäéô,’t
  *˜BqûÂ¾Ã?<_ÿú×ó‰O|"wİuW.¾øâ\|ñÅI’ªªªœqÆ9ï¼ó²ÿşûgòäÉ™;wn&Mš”ã?>IçˆÆc9&Ÿşô§sÑE¥µµ5§Ÿ~zN<ñÄ^¯ğ ½Õ–ä™ô,Ÿ‹ò  ’Bqû«¿ú«\{íµ9çœsòÕ¯~5“'OÎw¾óÌ=»ûœ/ùËiiiÉœ9s²aÃ†yä‘Y´hQFŒÑ}ÎW\‘ÓO?=üàS]]Y³feşüùEÜ ¤=É³Ù¾<l+2  ªR©äqB®©©)cÇMcccêëë‹À kO²:=ËÃg¢< *×9Iö):Äò»;]ŒP TG:Ÿqød’?ååò°µÀL  @ï)€~SJçêÊJgqøT:PÙR`&  `ç(€>QJ²6/‡]¯›‹  ô9…" ğ¦¼Ó–ŸN²±È@  À€P( oh]^.»F¶  (ŒB èa}z‡O&y©ĞD  @9Q(ÀÖ”íG6  ({
E "šÓ³<|2É†"  ƒ’B *Øú$?Oòx:Ÿƒ  °³Š P¡LòãX<  è[
E ¨0íI&¹)I©à,  @åQ(@Y—äÒtNq  è
E ¨Ë’\Sœ €ş¥P€A®=ÉµIn)Î  @ÿS(À ¶.É%I(:  0d(`2Å  (‚B Sœ €")`Y—äâ$«Š  Y
E $Hççç   †6…" ”¹ö$?OrKÑA   ¢P€²öb:Wq6Å  (
E (S÷'YSœ €ò¢P€2cŠ3  PÎŠ PF^Hçç?œ  àµ( L˜â  
E (X[:§8ßZt  €^P(@Lq  …" ä¿’\Sœ €ÁE¡ ¬-É5I~[t  €7A¡ è…$'y²è    o’B È}éœâ¼©è    ;A¡ ı¬-ÉÕIn+8  @_P(@?z>SœŸ*:  @Q(@?¹7É¿Çg   ²( µ¦sŠóâ¢ƒ   ô…" ô!Sœ €J§P€mÚwr[Ÿda’Í  ` )†€Ö$IJ¶uüÙëŸï½÷¯÷ı7{ìõ®Õ›B¯£—ç½Öw  xc
E€
Öšäö$¿Ng¡   ;K¡P‰   ô…"@iMò»t‰
Î  @eR(T E"   E¡0ˆ)  h
E€A¨5Éï“,Š"  €¥PD‰   M¡0t‰¿N²¾à,   m
E€2Ö–—G$*  (
E€2¤H   \)ÊH[’?$¹1ŠD   Ê“BT6%iIÒ¼íµ%ÉÖBõ–$·%YWp   x=
E mÙ¾üóÏ¯ö¾£ˆ°   @7…"ğ¦ı,Éª8ÿ•%â–~I   ô7…"ğ¦üg’[Š   ¸ê¢ ƒÏ½I®+:   P…"°CV%ùq’RÑA   €B(^[—ä‡IZ‹   F¡ôÊæ$?HÒTt    P
Eàu$¹4É3E   
§PŞĞÕI,:   PŠÀëZœäÖ¢C    eC¡¼¦‡“ü´è   @YQ(¯ê¹$§óù‰    ]ŠÀvš’|?+;   ¼’Bè¡5É…I^,:   P–Š@’<Qt    l)n×'¹»è   @YS(I’»’ÜPt    ì)<Î©Î    oD¡CÜé\„¥­è    À  P„!lS’ï'y©è    À ¡P¤Û7¾ñTUUåŒ3ÎèŞ·yóæœvÚiÙ}÷İ3fÌ˜Ìš5+kÖ¬éñ½§z*3gÎÌ¨Q£2~üø|éK_J[›ñnå®=É¿%Y]t   `PQ(’$¹ûî»óoÿöo9äCzìÿÂ¾ë¯¿>W_}u/^œç{.'œpB÷ñöööÌœ93[·nÍwÜ‘ä²Ë.Ë¼yóúØA?M²¼è   À £P$ÍÍÍ™={v.¹ä’ìºë®İûó£ı(ßúÖ·ò| ‡vX~üãç;îÈwŞ™$ùÍo~“Gy$?ùÉOrè¡‡æØcÍ×¾öµüà?ÈÖ­[‹º%ŞÀÍIn/:   0()Éi§–™3gfÆŒ=öß{ï½immí±ÿï|göŞ{ï,Y²$I²dÉ’|ğÁ™0aB÷9G}tšššòğÃ¿æ5·lÙ’¦¦¦cY’kŠ   ZµE X?ıéOsß}÷åî»ïŞîXCCC†–qãÆõØ?aÂ„444tŸóÊ2±ëx×±×rşùççÜsÏİÙøì §“\š¤Tt   `Ğ2Bq{úé§ó¿ş×ÿÊW\‘#FèµÏ9çœ466voO?ıô€^(jLòƒ$[Š   j
Å!ìŞ{ïÍÚµkó—ù—©­­Mmmm/^œùóç§¶¶6&LÈÖ­[³aÃ†ß[³fM&Nœ˜$™8qâv«>w}î:çÕ><õõõ=6úÏÖ$?L²¾è    À §PÂ>øÁæÁÌı÷ßß½M:5³gÏî~_WW—[n¹¥û;>úhzê©LŸ>=I2}úô<øàƒY»vm÷97İtSêëësàø=±½R’'ùSÁ9   €ÊàŠCØ.»ì’ƒ:¨Ç¾Ñ£Gg÷İwïŞÊ)§äÌ3ÏÌn»í–úúú|îsŸËôéÓó÷¼'IrÔQGåÀÌßÿıßç‚.HCCCşå_ş%§vZ†>à÷Äö&¹¯è   @ÅP(òº¾ıío§ºº:³fÍÊ–-[rôÑGç‡?üa÷ñšššÜpÃùÌg>“éÓ§gôèÑ9é¤“òÕ¯~µÀÔt¹#É¢¢C    ¥ªT*Yğ•Â555eìØ±illô<Å>òX’ï$i/:   Û9'É>E‡ØA~w§‹g(BZ“ä¢(  €¾gÊ3T­I’\š¤¥à,   @eR(Â Ó‘äÅt‡k’¬İö~m’é\Õ    ¿(¡L5¥³0üóí…$mæ   †6…"”™«’,I²©è     ¯B¡eä¦$·   àuXåÊÄƒI~^t   €7 P„2ğl:Wf¶ 
   PîŠP°¦$?H²¹è     ½ P„µ&¹(É‹E   è%…"èß“<^t   € P„‚üg’¥E‡    ØA
E(À}I®+:   À› P„öT’ÇŠÎ   Àà¤P„´!+:o-:   À›¤P„²5ÉÓY*   V
E ¥$—%y²à    ;K¡àú$÷    (¡Ÿİ•äWE‡    è#
EèGO$¹¼è    }H¡ıd]’“´    )¡lNòƒ$ME   èc
EècIş¿$Ï    (¡]›ä¢C    ô…"ô¡;’ü¦è    ıH¡}ä±$W    Ÿ)¡<Ÿäß’´    Ÿ)a'mJçŠÎÍE    
EØ	I.N²ºè     D¡;áº$   ` )a'<St    €¦P    zM¡    ôšB    è5…"    Ğk
E     ×Š    @¯)   €^S(    ½¦P    zM¡    ôšB    è5…"    Ğk
E     ×Š    @¯)   €^S(    ½¦P    zM¡    ôšB    è5…"    Ğk
E     ×Š    @¯)   €^S(    ½¦P    zM¡    ôšB    è5…"    Ğk
E     ×Š    @¯)   €^S(    ½¦P    zM¡    ôšB    è5…"    Ğk
E     ×Š    @¯)   €^S(qçŸ~şê¯ş*»ì²KÆŸã?>>úhs6oŞœÓN;-»ï¾{ÆŒ“Y³feÍš5=Îyê©§2sæÌŒ5*ãÇÏ—¾ô¥´µµä­    0 ŠCÜâÅ‹sÚi§åÎ;ïÌM7İ”ÖÖÖuÔQiiié>ç_øB®¿şú\}õÕY¼xq{î¹œpÂ	İÇÛÛÛ3sæÌlİº5wÜqG,XË.»,óæÍ+â–    èGU¥R©TtÊÇóÏ?ŸñãÇgñâÅyßûŞ—ÆÆÆ¼å-oÉ•W^™¿ıÛ¿M’üñÌ%K–ä=ïyOn¼ñÆ|ä#ÉsÏ=—	&$ùÿÛ»ó«ê³àÏì(0ƒ¢leµ¾ˆTƒŠ0N]k©˜Ô"ŠÕĞØ¢µÔZh«ÄÔ%—Äb*_*1*ÄV«N"¡©" hEYRDT[p‰@Ù™ù½(÷eÊâœæóINfæÜß=ç9÷É¹¿{¿÷Î½S¦L‰ë®».>şøã(..şÊı®_¿>ÊÊÊbİºuQZZÚ¨ÇØş7"^kî"   8àÜ½›»ˆz:PŸ»Óğ¼C‘:Ö­[‡~xDD,Z´(¶oßC†Éé×¯_ôìÙ3ªªª""¢ªª*?şø\˜1tèĞX¿~}¼ñÆ{ÜÏÖ­[cıúõu    Z>"9µµµqõÕWÇ©§Çw\DDTWWGqqqtèĞ¡ÎØÎ;GuuunÌ®aâÎËw^¶'&Lˆ²²²ÜÒ£G†>    @‘œ1cÆÄë¯¿>úh£ïë†nˆuëÖå–?ü°Ñ÷	   À×WØÜĞ2Œ;6fÎœ,ˆîİ»çÖwéÒ%¶mÛŸşyw)®Y³&ºté’³páÂ:ÛÛù-Ğ;Çü·’’’())ièÃ     ‘y‡b+—RŠ±cÇÆôéÓcîÜ¹Ñ§OŸ:—80ŠŠŠbÎœ9¹uË—/U«VEEEEDDTTTÄk¯½k×®Í™={v”––Fÿşı›æ@    hŞ¡ØÊ3&yä‘øë_ÿíÛ·Ï}æaYYYrÈ!QVVW\qEŒ7.?üğ(--_ıêWQQQ§œrJDDœsÎ9Ñ¿ÿøñ¿ÿıï£ºº:ÆcÆŒñ.D   €ƒŒ@±•»÷Ş{#"â¬³Îª³~êÔ©qÙe—EDÄ¤I“"???FŒ[·n¡C‡ÆÿøÇÜØ‚‚‚˜9sf\uÕUQQQmÛ¶Q£FÅm·İÖT‡   @ÉK)¥æ.Ö¯_eee±nİº(--mîr2ûßˆx­¹‹   à€sCDônî"êé@}îNÃóŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™6w   P_mSŠş‡}öYÔDÊÏšüü¨İ×’——ûY³óïK~~ÔäåEÊË‹Úˆ¨Ùå²šˆ¨ÍË‹yy±#"v|y9@k%P  à€ĞiÇ8ö_ÿŠ¾/¾]_x!òkjš­–”—;Ú´‰Ú’’¨mÓ&v|ù³¦¤$jŠ‹¿XJJbGqqÔE@î·”Ÿµ……QSTôÅÏÂÂ¨-(ˆš]/(ÈıÌıŸŸûY³KàœûûË`ygx\óeh¼ó÷š/äš/—Z=„"   -R^JÑ{ëÖè÷Î;ñ?Ï=‡¿öZs—”“—RmŞ±yss—BIyyQ[Xµ_†Æ©¸8v””Ä¶bËa‡ÅæbKiili×.6·kÿi×.6rHl()‰EEñŸ/K8  h1ŠRŠcÖ¯cŞx#¾9{vúÑGÍ]DÄ!rÁöíQ°}{ízÁdŞÆ–cc×®±¹S§èxöÙGÙàuBS(  Ğ¬ÚÕÖFÿ?c-ŠÏ>E76wIĞ(Ú|úi´ùôÓˆˆ()/(rÀ(  Ğä:ïØı?ü0şçÅ£Ë/D~mms—@FE   ]^JÑgË–8öwâ›sçÆáo¾ÙÜ%°ŸŠ4˜É“'ÇwŞÕÕÕ1`À€¸ç{bğàÁÍ]  ĞLŠw~âë¯Ç7Ÿy&©®nî’ h EÄc=ãÆ‹)S¦DyyyÜ}÷İ1tèĞX¾|ytêÔ©¹Ë  šHimmô_»6ú¾újô˜3'Š6mjî’ h`EÄ]wİ£GË/¿<""¦L™³fÍŠ|0®¿şúf®  hL]¶oÿâó_x!:WUù<D€ƒœ@‘¯mÛ¶m±hÑ¢¸á†rëòóócÈ!QUUµÇëlİº5¶nİšû{İºu±~ıúÆ-¶mˆmÍ]  4±ü”¢×æÍqÌ»ïÆQDÙÛoç.óıÌMÑ†Q|€=Şùœ=¥ÔÌ•ĞÜŠ|mŸ|òIÔÔÔDçÎë¬ïÜ¹s¼õÖ[{¼Î„	âÖ[oİm}=¥F   hQ¦Mkî
öÛ†¢¬¬¬¹Ë 	i7ÜpCŒ7.÷wmmm|öÙgÑ±cÇÈËËkÆÊÈjıúõÑ£GøğÃ£´´´¹Ë¡‘éwë£ç­·>zŞºèwë£ç­KSõ;¥6lˆnİº5Ú>80ùÚ8âˆ(((ˆ5kÖÔY¿fÍšèÒ¥Ë¯SRR%%%uÖuèĞ¡Ñj¤ñ”––z€ÒŠèwë£ç­·>zŞºèwë£ç­KSôÛ;‰ˆÈoî8ğÇÀcÎœ9¹uµµµ1gÎœ¨¨¨hÆÊ    hhŞ¡Hƒ7n\Œ5*N>ùä<xpÜ}÷İ±qãÆÜ·>   pp(¸å–[niî"8ğwÜqÑ¡C‡¸ıöÛcâÄ‰ñğÃÇ1ÇÓÌ•Ñ˜


â¬³ÎŠÂB¯M´úİúèyë£ç­·.úİúèyë¢ß4¥¼ä»¾   €Œ|†"    ™@    ÈL     d&P    2(ÂAdÂ„	1hĞ hß¾}têÔ)†Ë—/¯3fË–-1fÌ˜èØ±c´k×.FŒkÖ¬©3æ×¿şu80JJJâ„NØç>ßyçhß¾}tèĞá+ë[°`Aüà?ˆnİºE^^^<ùä“u.ß¾}{\wİuqüñÇGÛ¶m£[·nñ“Ÿü$>úè£Œ·@ëÓT=ÿı÷#//o·å¥—^úÊ'O½{÷6mÚDyyy,\¸p·1UUUqöÙgGÛ¶m£´´4Î8ãŒØ¼ys=oƒ_Kï·s¼á5åızJ)&Nœ}ûö’’’øÆ7¾·ß~ûWÖXYYıúõ‹6mÚÄñÇûÛßv³lÙ²6lX”••EÛ¶mcĞ A±jÕªzÜ­GKïùo¼#FŒˆŞ½{G^^^Ü}÷İûu|¡©ú}Ë-·ìñ~½mÛ¶_Y£y¼aµô›Ë^SŞ¯?ıôÓqÊ)§DûöíãÈ#Œ#FÄûï¿ÿ•5šËÙE8ˆÌŸ??ÆŒ/½ôRÌ=;¶oßçœsNlÜ¸17æšk®‰§z**++cşüùñÑGÅ…^¸Û¶~úÓŸÆ%—\²Ïımß¾=F§Ÿ~z¦ú6nÜˆÉ“'ïñòM›6ÅâÅ‹ã¦›nŠÅ‹ÇO<Ë—/aÃ†eÚ~kÔÔ=öÙgcõêÕ¹eàÀûÿØcÅ¸qãâæ›oÅ‹Ç€bèĞ¡±víÚÜ˜ªªª8÷ÜsãœsÎ‰…Æ+¯¼cÇü|SÔkéıv7¼¦ìùo~ó›¸ÿşûcâÄ‰ñÖ[oÅŒ3bğàÁû¬ïÅ_Œ‘#GÆW\K–,‰áÃ‡ÇğáÃãõ×_Ïy÷İwã´ÓN‹~ıúÅsÏ=ÿüç?ã¦›nŠ6mÚìÇ-rğké=ß´iSuÔQqÇwD—.]öûøBSõûÚk¯­s¾zõêèß¿üğ‡?Üg}æñ†×Ò{n.oxMÕó•+WÆùçŸgŸ}v,]º4~úéøä“Oö¸]™ËÙo	8h­]»6EDš?~J)¥Ï?ÿ<¥ÊÊÊÜ˜eË–¥ˆHUUU»]ÿæ›oNØëöûÛß¦K/½4M:5•••Õ«¶ˆHÓ§OÿÊq.L‘>øàƒzm¿µj¬¯\¹2EDZ²dI½ê<xp3fLîïšššÔ­[·4aÂ„Üºòòò4~üøzm—/´´~ïÊ9Ş8«ço¾ùf*,,Lo½õV½ê¹øâ‹Ó÷¿ÿı:ëÊËËÓ•W^™ûû’K.I—^zi½¶Ëÿki=ßU¯^½Ò¤I“ê}ì]c?vÛiéÒ¥)"Ò‚ö9Î<ŞøZZÏwe.oÕóÊÊÊTXX˜jjjrëfÌ˜‘òòòÒ¶mÛöZ¹œıåe#8ˆ­[·.""?üğˆˆX´hQlß¾=†’Ó¯_¿èÙ³gTUUÕkÛsçÎÊÊÊ½¾zÙPÖ­[yyy™ş¥šÆíyDÄ°aÃ¢S§NqÚi§ÅŒ3ö9vÛ¶m±hÑ¢:ûÎÏÏ!C†äö½víÚxùå—£S§Nñío;:wîgyf¼ğÂõ®­5jIıŞ_Îñúi¬?õÔSqÔQGÅÌ™3£OŸ>Ñ»wïøÙÏ~Ÿ}öÙ>¯WUUUgßC‡Íí»¶¶6fÍš}ûö¡C‡F§N¢¼¼|·¡cïZZÏ÷Ç{×Ø÷ë;İÿıÑ·oß}ş—‰y¼i´¤ï/syı4VÏùùù1uêÔ¨©©‰uëÖÅŸşô§2dHíõzærö—@RµµµqõÕWÇ©§Çw\DDTWWGqqñn“}çÎ£ºº:ó¶?ıôÓ¸ì²ËbÚ´iQZZÚ uïjË–-qİu×ÅÈ‘#u?‹Æìy»víâøCTVVÆ¬Y³â´ÓN‹áÃ‡ï3dúä“O¢¦¦&:wî¼×}¿÷Ş{ñÅçüŒ=:şş÷¿ÇI'ßıîwcÅŠ™ëkZZ¿÷‡s¼~³çï½÷^|ğÁQYY=ôPL›6--Z]tÑ>¯W]]½Ïs|íÚµñŸÿü'î¸ã8÷Üsã™g‰.¸ .¼ğÂ˜?~æúZ«–Øó†8ö¬1û½«-[¶ÄÃ?W\qÅ>Ç™Ç_KëùşnÛ\]cö¼OŸ>ñÌ3ÏÄ7Ş%%%Ñ¡C‡ø×¿ş?şø>¯g.g	á 5fÌ˜xıõ×ãÑGmğm=:~ô£Ågœ±ÇËŸşùh×®]nyøá‡ë½íÛ·ÇÅ_)¥¸÷Ş{¿nÉ­Bcöüˆ#ˆqãÆEyyy4(î¸ã¸ôÒKãÎ;ïŒˆıïymmmDD\yå•qùå—Ç‰'“&MŠc9&|ğÁ?ƒÉØï]9Çë¯1{^[[[·n‡z(N?ıô8ë¬³âˆyóæÅòåËcÕªUuzş»ßı.óv#"Î?ÿü¸æškâ„Nˆë¯¿>Î;ï¼˜2eJƒÇÁæ@ìùkÌc8Ø4Õm5}úôØ°aCŒ5*·Î<Ş<ÄïÊ\^Ùóêêê=ztŒ5*^yå•˜?~ÇE])%s9®°¹ ŞØ±ccæÌ™±`Á‚èŞ½{n}—.Ø×Jµ  #IDAT]bÛ¶mñùçŸ×ylÍš5{ı`õ=™;wnÌ˜1#&Nœ_|KdmmmÆ}÷İ#GŒ¥K—æÆÿ÷+^_eçƒ“>ø æÎëÕÎ»ç{R^^³gÏˆˆ“O>y·—””DAAÁnßP·ë¾»víıû÷¯3æØcõ­qûĞû]ÎñúkìwíÚ5
£oß¾¹uÇ{lDD¬Zµ*¾óïÔéùÎÓêÒ¥Ë>Ïñ#8"
÷xû—È}k©=oˆc`wMy¿~ÿı÷ÇyçWç¾Û<ŞôZbÏëÃ\^İóÉ“'GYYYüş÷¿Ï­ûóŸÿ=zôˆ—_~y·›Ëùº¼C")¥;vlLŸ>=æÎ}úô©sùÀ£¨¨(æÌ™“[·ó]™÷SUUK—.Í-·İv[´oß>–.]\pArÈ!qôÑGç–öíÛgŞöÎ'+V¬ˆgŸ}6:vì˜ùº­QSõ|O–.]š{"±§ÇÀëì»¶¶6æÌ™“ÛwïŞ½£[·n±|ùò:Û~ûí·£W¯^_«¾ƒQKîwVÎñúiªŸzê©±cÇx÷İwsëŞ~ûíˆˆèÕ«WÖéùÎ'!uö1{öìÜ¾‹‹‹cĞ AÎñzhé=oˆcàÿ5õıúÊ•+cŞ¼y»ıë«y¼é´äge.¯Ÿ¦êù¦M›vûvõ‚‚‚ˆˆÜ›?Ìå4¨æø& q\uÕU©¬¬,=÷ÜsiõêÕ¹eÓ¦M¹1¿øÅ/RÏ=ÓÜ¹sÓ«¯¾š***REEEí¬X±"-Y²$]yå•©oß¾iÉ’%iÉ’%iëÖ­{ÜoÖoyŞ°aCn[‘îºë®´dÉ’Ü7ÂmÛ¶-6,uïŞ=-]º´Î1ìmß­]Sõ|Ú´ié‘GIË–-KË–-K·ß~{ÊÏÏO>øà>ë{ôÑGSIIIš6mZzóÍ7ÓÏşóÔ¡C‡T]]3iÒ¤TZZš*++ÓŠ+ÒøñãS›6mÒ;ï¼Ó€·ÔÁ¡¥÷Û9Şğšªç555é¤“NJgœqFZ¼xqzõÕWSyyyúŞ÷¾·Ïúşñ¤ÂÂÂ4qâÄ´lÙ²tóÍ7§¢¢¢ôÚk¯åÆ<ñÄ©¨¨(İwß}iÅŠé{îIéùçŸoÀ[êàÑÒ{¾uëÖÜ¶ºvíš®½öÚ´dÉ’´bÅŠz_hêÇnãÇOİºuK;vìÈTŸy¼áµô›Ë^Sõ|Îœ9)///İzë­éí·ßN‹-JC‡M½zõÚçı¯¹œı%P„ƒHDìq™:ujnÌæÍ›Ó/ùËtØa‡¥C=4]pÁiõêÕu¶sæ™gîq;+W®Üã~³ŠóæÍÛãvG•RJiåÊ•{=†yóæíç­rpkªO›6-{ì±éĞCM¥¥¥iğàÁ©²²2S÷ÜsOêÙ³g*..NƒN/½ôÒnc&L˜ºwï=ôĞTQQáÁÉ^´ô~;Ç^SŞ¯ÿûßÿN^xaj×®]êÜ¹sºì²ËÒ§Ÿ~ú•5>şøã©oß¾©¸¸8}ë[ßJ³fÍÚmÌ<>úèÔ¦M›4`À€ôä“Oî÷mr°ké=ßÛy|æ™gÖëøBSö»¦¦&uïŞ=İxãõªÑ<Ş°ZzÏÍå¯){ş—¿ü%xâ‰©mÛ¶éÈ#LÃ†KË–-ûÊÍåì¼”R
    €|†"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    ÈL     d&P    2(    ™	   €ÌŠ    @fE     3"    ™@    Èìÿ EÔï4ÑŒ­    IEND®B`‚
```
